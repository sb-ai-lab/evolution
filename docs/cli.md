# `evo` CLI Reference

Use this page to look up command syntax and output. For a first tree campaign, follow the [Quickstart](../QUICKSTART.md); for day-to-day use, read [Running campaigns](running-campaigns.md). Jump to [Tree Service Commands](#tree-service-commands) for `evo tree`. The other command groups support the separate [task/skill workflow](manual-workflow.md).

```bash
evo --help
evo <group> --help
evo <group> <command> --help
```

Install the intended version using the [installation guide](../QUICKSTART.md#1-install-the-beta). `evo <group> --help` and `evo <group> <command> --help` are authoritative for the installed version.

## Edit Brake Controls

These controls apply to the task/skill rewrite command, not the tree campaign editor.

Use `evo evolve rewrite <skill> --version-decay --max-rewrite-attempts 1` for the default policy. The `--version-decay` flag enables promotion-count decay. Use `--no-version-decay` for a constant budget. The attempt value is the total editor-call count. Evolution retries only after `candidate_edit_budget_exceeded`.

## Command Tree

| Group | Commands |
|---|---|
| `evo task` | `new`, `list`, `setup`, `eval`, `eval-all`, `import-sb-bench` |
| `evo skill` | `create`, `list`, `show`, `validate`, `search`, `status`, `lineage`, `diff`, `history`, `commit`, `promote`, `discard`, `repair`, `delete-version`, `import`, `import-skillsbench` |
| `evo skill container` | `create`, `list`, `show`, `delete`, `snapshot`, `snapshots` |
| `evo run` | `solve` |
| `evo run bench` | `run`, `matrix` |
| `evo trace` | `list`, `show`, `repair` |
| `evo evolve` | `reflect`, `rewrite`, `optimize`, `status` |
| `evo split` | `check`, `list` |
| `evo tree` | `evolve`, `conformance`, `authorize`, `evaluate`, `trace`, `watch` |
| top level | `init`, `doctor`, `quickstart` |

## Shared Behavior

### Task roots

Commands that load tasks resolve the root in this order:

1. explicit `--dir PATH` (or the Python `tasks_dir` argument);
2. `EVO_TASKS_ROOT`;
3. `tasks/` under the current working directory.

### Models and credentials

`--model`, `--solver-model`, and `--editor-model` accept LiteLLM model ids, `mock`, `gigachat`, or agent CLI ids such as `codex/...`, `claude-code/...`, and `hermes/...`. `--api-base URL` selects an OpenAI-compatible endpoint.

For `evo evolve optimize`, `--solver-api-base` and `--editor-api-base` select independent endpoints and each overrides the shared `--api-base` fallback. Explicit `--solver-temperature`/`--editor-temperature` and `--solver-reasoning-effort`/`--editor-reasoning-effort` values override the legacy `EVO_LLM_TEMPERATURE` and `EVO_LLM_REASONING_EFFORT` environment fallbacks for that role. Temperatures must be finite values in `[0, 2]`; reasoning effort is `off`, `low`, `medium`, `high`, or `xhigh`.

Provider credentials are read only from their environment variables, including `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, and `GIGACHAT_CREDENTIALS`. The CLI intentionally has no credential option. Local OpenAI-compatible endpoints are handled without asking for a dummy key.

The output budget resolves as explicit `--max-tokens`, then `EVO_LLM_MAX_TOKENS`, then `16384`.

### Permissions and child environments

Agent CLI calls default to `--permission safe`. Bypassing the backend's safety controls requires `--permission unsafe` on that command. All safe Hermes calls use file tools only, confine writes to isolated run directories, and fail closed when the required Linux containment is unavailable.

Generated programs and task evaluators receive a restricted child environment. `--pass-env NAME` passes one additional variable by name and is repeatable:

```bash
evo run solve my-task -m openai/local-model \
  --api-base http://127.0.0.1:8000/v1 \
  --pass-env HTTPS_PROXY --pass-env SSL_CERT_FILE
```

The option is available on solve, benchmark, task evaluation, quickstart, and the optimizer where those commands launch child task processes.

### Exact skill bindings

By default, task runs inject the active version of every required skill. They do not read uncommitted working content. Each binding records its version and a SHA-256 over the complete managed tree (`SKILL.md`, `scripts/`, `references/`, and `assets/`). Missing snapshots or hash mismatches fail closed.

For a multi-skill task, `evo run solve --skill REPLACEMENT` replaces only the primary requirement. Supporting requirements are preserved and pinned. A container snapshot similarly resolves every member to its recorded version and hash.

### JSON output

Commands with `--json` write exactly one envelope to stdout. Human diagnostics go to stderr.

Successful envelope:

```json
{"schema_version": 1, "ok": true, "data": {}}
```

Failure envelope:

```json
{"schema_version": 1, "ok": false, "error": {"kind": "error_kind", "message": "description"}}
```

`data` may be an object or list. Do not parse human table output or depend on undocumented fields outside this envelope.

## Task Commands

```bash
evo task new NAME [--dir PATH] [--skill NAME] [--force]
evo task list [--dir PATH]
evo task setup NAME --workspace PATH [--dir PATH]
evo task eval NAME --workspace PATH [--dir PATH] [--pass-env NAME]...
evo task eval-all --workspace PATH [--dir PATH] [--skill NAME] \
  [--pass-env NAME]...
```

`new` creates `task.toml`, `instruction.md`, `inputs/`, and `tests/test_outputs.py`. `setup` materializes inputs without solving. `eval` and `eval-all` run task evaluators against an existing workspace.

`evo task import-sb-bench REPO` imports a selected SB-Bench corpus. See command help for role, difficulty, task, skill, container, and overwrite filters.

## Skill Commands

```bash
evo skill create NAME -d DESCRIPTION
evo skill list [--status STATUS] [--json]
evo skill show NAME [--version VERSION] [--json]
evo skill validate NAME_OR_PATH
evo skill diff NAME [V1] [V2]
evo skill commit NAME [-m MESSAGE] [--no-promote]
evo skill promote NAME VERSION
evo skill discard NAME
evo skill history NAME
evo skill lineage NAME
```

`commit` snapshots the full managed skill tree and normally makes the new version active. `--no-promote` records a saved candidate version without switching active content. `promote` atomically restores the selected full tree. `discard` restores the active tree over the working copy.

Maintenance and import commands:

```bash
evo skill search QUERY [--limit N]
evo skill status
evo skill repair NAME [--dry-run]
evo skill delete-version NAME VERSION [--force]
evo skill import SOURCE [--name NAME]
evo skill import-skillsbench REPO
```

Container commands are nested under `skill`:

```bash
evo skill container create NAME -d DESCRIPTION --skills a,b
evo skill container list
evo skill container show NAME
evo skill container snapshot NAME -m MESSAGE
evo skill container snapshots NAME
evo skill container delete NAME
```

## Run And Trace Commands

Solve one task and record one trace per exact skill binding:

```bash
evo run solve TASK -m MODEL [--skill NAME] [--no-skill] \
  [--workspace PATH] [--dir PATH] [--api-base URL] \
  [--max-tokens N] [--seed N] [--timeout SECONDS] [--snapshot LABEL] \
  [--permission safe|unsafe] [--pass-env NAME]... [--json]
```

The non-negative `--seed` is accepted only when the solver backend explicitly supports seed forwarding and is then recorded as `attempt_seed` in trace provenance. Agent-CLI and GigaChat solver calls reject an explicit seed; supported provider runtimes may still be nondeterministic.

With a split manifest, `--manifest-scope` is required:

```bash
evo run solve TASK -m MODEL --manifest split.json \
  --manifest-scope skill_evolution
```

Run repeatable measurements:

```bash
evo run bench run -m MODEL [--tasks a,b] [--trials N] \
  [--skill-version VERSION | --no-skill] [--dir PATH] \
  [--results-dir PATH] [--permission safe|unsafe] [--pass-env NAME]...

evo run bench matrix --models MODEL_A,MODEL_B \
  [--conditions no-skill,latest] [--tasks a,b] [--trials N]
```

Inspect or repair traces:

```bash
evo trace list SKILL [--version V] [--model MODEL] [--task TASK] [--json]
evo trace show SKILL [TRACE_ID] [--last] [--section prompt|response|code|result]
evo trace repair SKILL
```

## Manual Evolve Commands

```bash
evo evolve reflect SKILL [-m MODEL] [--task TASK] [--limit N] \
  [--trace-model MODEL] [--manifest PATH] [--dir PATH] \
  [--api-base URL] [--max-tokens N] [--permission safe|unsafe]

evo evolve rewrite SKILL [-m MODEL] [--task TASK] \
  [--trace-model MODEL] [--skill-version VERSION] \
  [--rewrite-mode text-return|file-edit|agentic-session|edit-ops] \
  [--version-decay|--no-version-decay] [--max-rewrite-attempts N] \
  [--dry-run] [--manifest PATH] [--dir PATH] \
  [--api-base URL] [--max-tokens N] [--permission safe|unsafe] \
  [--include-best-trace-code|--no-best-trace-code] \
  [--rewrite-audit-out PATH] [--rewrite-row-id ID]
```

Reflection reads eligible failing traces. Rewrite produces a checked candidate; an eligible candidate updates only the working tree. Run `evo skill diff`, then commit before the default solver can inject it. `agentic-session` requires one `--task` and is not supported by the multi-task optimizer.

## Tree Service Commands

The `evo tree` surface has six commands: [conformance](#evo-tree-conformance) checks an operator-started Target Service; [evolve](#evo-tree-evolve) runs or resumes a campaign; [authorize](#evo-tree-authorize) records acceptance of eligible campaign refusal findings; [evaluate](#measure-a-tree-on-train-or-test) measures a frozen tree on train or test; [trace](#evo-tree-trace) exports one provenance record per try; and [watch](#evo-tree-watch) follows a run read only. Start the target first; Evolution never starts it.

```bash
evo tree conformance --manifest ./demo/manifest.json
evo tree evolve --manifest ./demo/manifest.json --run-dir /tmp/evolution-runs/run-1
```

The manifest is the whole configuration surface. It declares the target binding, the seed root, the artifact policy, the campaign policy, the Environment, the scorer command, the editor command and the corpus; there is no command-line override of anything it declares. See [Manifest v2](manifest-v2.md) for the document, its sections, the corpus row format and the two program contracts.

### `evo tree evolve`

```bash
evo tree evolve --manifest PATH --run-dir DIR [--quiet] [--json-indent N] [--progress]
```

`--manifest` is the complete `evolution-v2-manifest/15` or opt-in `evolution-v2-manifest/16` document and `--run-dir` the campaign's own run directory. Every other campaign schema is refused `manifest_schema`, for a new run, resume or finished reopen, with `expected evolution-v2-manifest/15 or /16`, before the run directory is read. [The refusal list](manifest-v2.md#refusals) enumerates the removed schemas. `--quiet` prints only `terminal`, `finished` and `code` on stdout and leaves stderr unchanged; `--json-indent` sets the JSON indent (`2` by default; `0` prints one line per key with no indent). Neither changes campaign semantics.

Campaign commands and library readers support only families `14` and `15`. Removed families and unversioned creation records are refused `campaign_family_unsupported at family/N` (`family/absent` when unversioned), exit `2`, before target contact or writes. The guidance is `this release reads campaign families 14 and 15; read this run with evolution 2.0.0b9 or seed a new /15 campaign from its champion copy`. Missing or illegible creation lines keep their missing/unreadable outcomes, not an old-family refusal. New links admit family 14 for a `/15` successor, or families 14 and 15 for `/16`; an older predecessor is `lineage_family_unsupported`. Existing current-family runs with a recorded family-13 predecessor still read their own journals and resume. An owed baseline import from that predecessor reports `BaselineImportUnavailable` (`baseline_import_unavailable`); accept the printed decision to use `fresh_baseline`. See [linked runs](#linked-runs).

Add `--progress` to print live status lines on stderr. It is off by default, has no manifest field and changes no run evidence, role input or exit code. It uses the same view as [`evo tree watch`](#evo-tree-watch), including under `--quiet`, and drains before the final report. Diagnostic notes print immediately on stderr, including before progress starts. A refused invocation discards its pending progress; if another process is already writing this directory, its new records can trigger lines before this invocation refuses.

The command copies stdout and stderr into `<run_dir>/logs/evolve-<UTC start>-<pid>.log`, created `0600` in a `0700` directory. The copy uses the run's secret redaction and includes progress and notes in terminal order, an unhandled traceback and a closing `exit` line. It starts only after this invocation passes its last refusal point: a refused campaign or a crash before that point leaves no log. An unfinished resume gets a log; a finished reopen gets one only if it appends to the campaign journal, not for documentation or store repairs alone. A broken or missing stdout or stderr stops receiving output while the log keeps the copy; it no longer ends the invocation. Logs are not evidence and no reader or role uses them. Python calls of `evolve`, including an in-process Evolution Lab worker, create no log and install no process-wide capture. See [storage and limits](run-storage-layout.md#the-v2-run-directory).

Resume is a fact of the run directory, not a flag. An absent or empty directory starts a new campaign (an absent one under a parent that does not exist is refused with `run_dir_parent_missing`: the command creates its run directory and never an ancestor); a directory holding `campaign.jsonl` continues the campaign journaled there; a directory holding anything else, ignoring the workflow's own four output roots (`scorer-traces/`, `editor-logs/`, `reflector-logs/` and `environment-docs/`, and `connection-logs/` and `logs/` for a `/15` run), is refused with `run_dir_not_campaign` rather than laid down on top of somebody else's files. Ordinary continuation rebuilds every binding from the manifest and refuses with `run_identity_changed` when the document no longer yields the identity the journal recorded, so a campaign is never silently continued under a changed configuration. This producer writes and resumes family `14` from a `/15` document or family `15` from a `/16` document. A run directory of family `3` to `13`, or one without a family, is refused `campaign_family_unsupported at family/N` before any write, and there is no converter or writable historical mode. Releases before 2.0.0b2 wrote families `6`–`13` from `/5`–`/14`. A run directory whose journal cannot be read — an empty `campaign.jsonl`, a creation record torn by a crash, or a journal file the process cannot read at all, its permissions removed or its size past the reader's bound — is resumed and reported by the campaign as `persistence_failed` (exit 1), not refused: the campaign is present, only its journal is illegible. The outcome is still one JSON document on stdout, never a traceback.

A `run_identity_changed` the operator authorized is not a refusal but a rebase: the campaign journals `identity_rebased`, the artifact ledger journals `store_rebased`, and one `authorization_applied` records every decision the invocation consumed. Later resumes compare the rebuilt identity against the latest rebase rather than against the creation record, every measurement bound afterwards derives from it, and a sibling journal written by any admitted producing version is resumed by its owner. `campaign_result` and `campaign_status` report `identity_digest` and `rebases`, so the run says which identity it is running under and how it got there. The two records are written in that order and each pair is completed before the next is declared: a resume that finds an `identity_rebased` whose `store_rebased` a crash never reached writes the missing ledger record first, stamped with the producing version of the campaign record it completes, and only then appends anything of its own. Admission itself still writes nothing: a run directory whose `environment-docs/` snapshot was lost while the live documentation still has the journaled digest is repaired by the campaign after its records, so an invocation refused for some other difference leaves the directory exactly as it found it.

Resume requires the producing package version recorded by the campaign, or an explicit authorized producing-version rebase. A policy-identity mismatch is refused before torn-tail repair or other mutation. Measurement identity retains its separate `package_version_mismatch` refusal. A changed corpus partition is a hard refusal and cannot be admitted by a rebase.

A `/15` or `/16` manifest that declares [`verbatim_answers`](manifest-v2.md#verbatimanswers) is checked once against the train corpus when the run is bound. An entry that contains a forwarded secret value is refused `record_matches_secret`, and an entry that equals no oracle string value of the train cases is refused `verbatim_answer_unmatched`. A value that appears only in test rows does not count, and an entry shorter than eight characters can never match. Each refusal names the entry as `verbatim_answers.strings[i]`, exits `2` and writes nothing. The value is never printed. Adding, changing or removing the declaration on a resume is refused `run_identity_changed` and names `identity.verbatim_answers`. It is not a comparable field, so accepting it takes the plain `evo tree authorize` command that the refusal prints, and the campaign continues with the new list.

A `/16` manifest can also declare [`secret_oracle_paths`](manifest-v2.md#secretoraclepaths). It limits oracle needles to selected subtrees; `pointers: []` disables only the oracle check, with the cost that unprotected reference answers may reach the Editor. Run-secret checks remain enabled. Adding, changing or removing it on resume gives `run_identity_changed` at `identity.secret_oracle_paths`; accept the printed authorization with `rebase`, then repeat the same command.

| Code | Location and action |
|---|---|
| `record_matches_secret` | `secret_oracle_paths.pointers[i]` contains a run secret. Remove that secret from the declaration. |
| `secret_oracle_path_unmatched` | `secret_oracle_paths.pointers[i]` selects no string value of at least eight characters in train. Correct or remove the pointer; a match only in test rows does not count. |
| `unknown_key`, `missing_key`, `bad_type`, `bad_value` | A `secret_oracle_paths` shape fault; use the fixed `expected` sentence. `/15` refuses the key as `unknown_key`. |

These refusals exit `2` before writes and never print the pointer or a value.


Optional live probes are configured only by `reflector.probe`, with no CLI or environment override. Start an isolated disposable environment API at another loopback port; see the [probe contract](manifest-v2.md#optional-live-probes). Unavailable health or local mediator setup suspends before reflection; resume reruns the check. Changed probe configuration is a non-comparable `run_identity_changed`: accepting it rebases an unfinished campaign, while a linked difference adds no condition caveat. Resetting external data at the same origin is not identity-checked, and there is no coordinated safe reset hook. The reference sample uses `probe: null` and needs only init, serve, conformance and evolve.

A new run takes its campaign `run_id` from the run directory's name: lower-cased, every character outside `[A-Za-z0-9._-]` replaced by `-`, truncated to 93 characters and prefixed `c-`, so the complete id is at most 95 characters. That id prefixes every instance the campaign opens on the target, so a readable run-directory name is a readable trace on the target's side. A resume reads the id the journal recorded and never derives a new one.

The run directory must be disjoint from the seed root and from both program working directories — neither equal to nor inside the other, in either direction — otherwise the command refuses with `run_dir_overlaps_seed` or `run_dir_overlaps_program`. The run directory is canonicalized before any of that: a symlinked ancestor — `/tmp` on macOS, a symlinked home directory — is resolved and the run proceeds, while a run directory that is itself a symlink is refused with `run_dir_unsafe_entry at run_dir`. That entry is named `run_dir` whatever the directory is called: the output roots print their own fixed names and the run directory prints this one, so the token is always a journal identifier and never an operator's file name. Every refusal happens before anything is written under the run directory, so a refused invocation leaves it exactly as it found it and the same command can be retried.

Stdout is one JSON document built from the campaign result and passed through the redactor, with `terminal`, `quiescent`, `finished`, `suspended`, `owner`, `code`, `result` and `status`. `terminal` is `finished`, `suspended`, `storage_capacity_exhausted` or `persistence_failed`. `finished` is the `campaign_finished` payload, `null` for every other terminal; `suspended` is its family-7/family-8/family-9/family-10 counterpart, the `campaign_suspended` payload this run wrote (`reason`, `owner`, `evidence`) and `null` otherwise. `result` is the `campaign_result` projection, or `null` when no result is available, including `projection_invalid`; `status` is the `campaign_status` projection. The values of every environment name the manifest's `allowed_env` sets declare are redacted from the document, as they are from the journal.

From `2.0.0b7`, a low inode estimate warns and requested work continues; earlier b5/b6 releases retain their forecast-only `storage_capacity_exhausted` result with code `storage_inodes_insufficient`. Historical records remain readable. New scripts must handle actual storage faults and surface the warning instead of relying on that forecast terminal as a pre-measurement stop. The CLI prints notes immediately on stderr, including with `--progress`, and copies them to an admitted process log. Warning prose is not a machine API and adds no stdout field. Actual trace-write faults still print the failed file and return a storage terminal; see [storage recovery](running-campaigns.md#when-the-disk-or-its-inodes-run-out).

Every current round entry carries `reason`: `reflection_declined`, `reflection_empty` or content-class `reflection_failed` for `unplanned` rounds without an Editor Attempt, and null otherwise ([unplanned rounds](manifest-v2.md#unplanned-rounds-family-9)). Provider and unknown reflection failures suspend without closing the round. `grouping_fallback` caveats name rounds whose Grouper used a fallback. Neither field changes the exit code.

#### Affected-case reports (family 12)

Read historical family-12 reports with `2.0.0b9`; current campaigns retain the affected-case fields below ([current `/15` reference](manifest-v2.md#stable-family-keys-manifest-15)).

#### Shared affected-case report fields

Every Attempt in `rounds[].attempts[]` of `status` and `result` carries `affected`.

It is `null` until the Attempt's case selection is journaled. After that it holds `source` (`reported`, `metadata` or `none`), `mode` (`component`, `path` or `null`), `fallback_reason` (`null` when no fallback happened), `layer_one` and `layer_two` (the number of cases in each layer) and `fresh_parent` (whether a fresh Parent measurement was bound for the Attempt).

It also holds `alpha_statement: null`; current campaigns spend no alpha.

The reasons and scopes are explained in [affected-case selection](manifest-v2.md#affected-case-selection).

The top-level `caveats` list gains four kinds after every existing item, ordered by round, Attempt and kind.

`focus_selection_bias` has the keys `kind`, `round` and `attempt_id`. In `/15` it appears once for each measured Attempt when `fresh_parent` is off, whatever `global` is.

`completion_regression` is not reported by current campaigns: finalists require full-train evidence before Promotion.

`provenance_drift` has `kind`, `round`, `attempt_id`, `measurement_id` and `cases` (case ids). It applies under `reported` provenance when a finalist's final-measurement row outside its local measured set used a changed part. It is unavailable after `changes_unrecorded`, because only a changed-part count remains.

`components_changed_by_condition` has `kind`, `round`, `attempt_id` and `sides`: the Candidate, Parent or final-measurement sides that rendered a different component map after accepted condition drift. Inside the reference epoch the same difference suspends `target_components_changed`; a missing map names no side. Current side names are listed under [final-measurement reports](#final-measurement-reports-family-14).

The final-measurement fields below describe current provenance caveats; none is a per-case non-regression guarantee.

None of them changes a statistic, a decision or an exit code.

They never reach the editor's or the Reflector's inputs.

#### Confidence and cohort reports (family 13)

Read historical family-13 reports with `2.0.0b9`; use the [current `/15` final-measurement reports](#final-measurement-reports-family-14) and the confidence fields below.

#### Confidence report fields (manifest /15)

In `/15`, the top-level `alpha`, each Attempt's `alpha_total` and `affected.alpha_statement` are `null`. The `confidence` fields below describe the stable-family gate; `/16` uses its independent comparison reports instead.

Every `/15` Attempt in `rounds[].attempts[]` carries `confidence`, null until its comparison is journaled. This partial example illustrates the shared statistical fields with four repeats, one focus case and one causal case; the complete current shape also has `margin` and the `exact` estimate fields described [below](#final-measurement-reports-family-14):

```json
{
  "calibration": null,
  "causal_cases": 1,
  "confidence": {
    "causal": 0.9,
    "focus": 0.9
  },
  "evaluated": [
    {
      "causal": null,
      "caveats": [
        "intervals_empirical"
      ],
      "comparison": 22,
      "decision": "continue",
      "final": false,
      "focus": {
        "level": 0.9,
        "lower": 0.5333333333333333,
        "point": 0.6,
        "unavailable": null,
        "upper": 0.6666666666666667
      },
      "look": 3,
      "overall": null,
      "reason": null,
      "tolerance": {
        "source": "numeric",
        "value": 0.05
      },
      "would_pass_point": null
    },
    {
      "causal": {
        "level": 0.9,
        "lower": -0.007499999999999951,
        "point": 0.00250000000000003,
        "unavailable": null,
        "upper": null
      },
      "caveats": [
        "intervals_empirical"
      ],
      "comparison": 22,
      "decision": "finalist",
      "final": true,
      "focus": {
        "level": 0.9,
        "lower": 0.575,
        "point": 0.65,
        "unavailable": null,
        "upper": null
      },
      "look": 4,
      "overall": {
        "level": 0.9,
        "lower": 0.28875,
        "point": 0.32625000000000004,
        "unavailable": null,
        "upper": null
      },
      "reason": "gate_passed",
      "tolerance": {
        "source": "numeric",
        "value": 0.05
      },
      "would_pass_point": null
    }
  ],
  "focus_cases": 1,
  "looks": [
    3,
    4
  ],
  "method": {
    "draws": 20000,
    "noise_level": 0.995,
    "quantile": "linear",
    "version": "f13-bootstrap/1"
  },
  "mode": "statistical",
  "parent_measurement_id": "m-r01-a01-parent",
  "parent_source": "fresh_parent",
  "repeats": 4,
  "retained": {
    "causal": 1,
    "focus": 1
  },
  "tolerance": {
    "kind": "numeric",
    "value": 0.05
  }
}
```

The keys of `confidence` are:

- `mode`: `statistical` or `observe`.
- `parent_source`: `fresh_parent` or `baseline`, the Parent the comparison used. Its `parent_measurement_id` names that measurement.
- `focus_cases` and `causal_cases`: the sizes of F and C. `retained` gives the sizes that remain after pair exclusions, as `{"focus": n, "causal": n}`.
- `repeats`: R. `looks`: the planned look schedule, `[3, 4]` here, and `[R]` in `observe` (`[1]` in the example below).
- `confidence`: `{"focus": c, "causal": c}`, or `null` in `observe`.
- `tolerance`: `{"kind": "numeric", "value": x}` or `{"kind": "measured_noise"}`.
- `method`: the interval method, with `draws`, `noise_level`, `quantile` and `version`.
- `calibration`: `null` until the Parent-only noise estimate is journaled. Only `tolerance: measured_noise` with a non-empty C owes one, unless the Attempt closed at binding. A numeric tolerance, or an empty C, keeps it `null`. Once journaled it has `allowance`, `degenerate` and `unavailable`, which is `null` or `insufficient_calibration_repeats`.
- `evaluated`: every evaluated look in order. Each look cites the `comparison` record it was decided under, so a look decided before a `continue` stays in the report.

Each evaluated look has `look`, `comparison`, `final`, `decision`, `reason`, `would_pass_point`, `caveats` and estimates `focus`, `causal`, `overall`. Estimate fields are `point`, `lower`, `upper`, `level`, `unavailable`; focus and causal also have `exact`. Causal and overall are null at an interim look, and causal is null for empty C. `unavailable` can be null, `insufficient_repeats` or `not_requested_in_observe_mode`; no within-case variation uses the exact contrast, not `degenerate_interval`. The look's `tolerance` records `source` (`numeric` or `measured_noise`) and `value`.

The `decision` is `continue`, `rejected`, `finalist` or `insufficient_evidence`, with the [current gate reasons](manifest-v2.md#the-candidate-gate-in-15). `observe` uses `finalist` with `observe_finalist` unless a critical failure rejects it. Its estimates have point values and no bounds. The following legacy example illustrates those shared point fields only; current `/15` estimates also carry `exact`, and its no-variation caveat is `noise_not_observed`:

```json
{
  "causal": {
    "level": null,
    "lower": null,
    "point": 0.0,
    "unavailable": "not_requested_in_observe_mode",
    "upper": null
  },
  "caveats": [
    "noise_allowance_empirical",
    "observe_calibration_degenerate",
    "observe_gate_not_enforced",
    "uncalibrated_point_estimates"
  ],
  "comparison": 21,
  "decision": "finalist",
  "final": true,
  "focus": {
    "level": null,
    "lower": null,
    "point": 1.0,
    "unavailable": "not_requested_in_observe_mode",
    "upper": null
  },
  "look": 1,
  "overall": {
    "level": null,
    "lower": null,
    "point": 0.5,
    "unavailable": "not_requested_in_observe_mode",
    "upper": null
  },
  "reason": "observe_finalist",
  "tolerance": {
    "source": "measured_noise",
    "value": 0.0
  },
  "would_pass_point": true
}
```

`would_pass_point` is what the point values would have decided. It is `null` in `statistical` and when the calibration is unavailable. An `observe` Attempt closed before any look was evaluated (`no_evaluable_pairs`) has `evaluated: []` and no method caveat. None of these values changes a decision, because decisions are taken on the published values.

Current rounds carry `final`, not the historical `cohort` report; see the [current fields and example](#final-measurement-reports-family-14). Read removed-family evidence with `2.0.0b9` and its matching CLI documentation.

#### Final-measurement reports (family 14)

In a family-`14` campaign (manifest `/15`), `status` and `result` carry the confidence fields above and the final-measurement fields below. `alpha` is `null`. The values below come from the scaffold's run ([a `/15` smoke campaign](running-campaigns.md#a-15-smoke-campaign)).

Each Attempt's `confidence` block has family 13's keys plus `margin`, the focus margin its comparison was bound with. A `focus` or `causal` estimate has a sixth key, `exact`: `true` when the comparison saw no within-case variation and was decided on its point, `false` otherwise, and `null` in `observe`. An exact estimate has `lower` equal to `point`. `overall` keeps five keys. A look's `decision` is `continue`, `rejected`, `finalist` or `insufficient_evidence`: a Candidate that passes is a `finalist`, with the reason `gate_passed` or `observe_finalist`. A focus rejection has the reason `focus_below_margin` at every look, and `degenerate_interval` does not occur:

```json
{
  "margin": 0.0,
  "evaluated": [
    {
      "decision": "finalist",
      "reason": "gate_passed",
      "focus": {
        "exact": true,
        "level": 0.9,
        "lower": 1.0,
        "point": 1.0,
        "unavailable": null,
        "upper": null
      }
    }
  ]
}
```

Each round carries `final`, a list with one entry per final measurement in journal order. It is `[]` until the round's first finalist is bound, and in a round without finalists. Under `all_attempts` a round has at most one entry; under `first_accepted` it has one for each finalist. Round 1 of the scaffold's run:

```json
[
  {
    "champion_side": "stored",
    "condition_changed_in_place": false,
    "contrasts": [
      {
        "accepted": true,
        "attempt_id": "r01-a01",
        "condition_changed_in_place": false,
        "exact": true,
        "level": 0.9,
        "lower": 1.0,
        "pairing": "exact",
        "point": 1.0,
        "reason": "global_point_improved",
        "removed_cases": 0,
        "removed_pairs": 0,
        "unavailable": null
      }
    ],
    "global": "point",
    "listed": [],
    "members": [
      {
        "attempt_id": "r01-a01",
        "complete": true,
        "critical": false,
        "measurement_id": "m-r01-final-01-a01",
        "missing": 0,
        "point": 1.0,
        "role": "finalist"
      }
    ],
    "mode": "statistical",
    "next_baseline_measurement_id": "m-r01-final-01-a01",
    "ordinal": 1,
    "selection": {
      "attempt_id": "r01-a01",
      "measurement_id": "m-r01-final-01-a01",
      "reason": "finalist_selected"
    },
    "stability": "stable",
    "stored_measurement_id": "m-r00-champion"
  }
]
```

An entry has `mode`, `global`, `members`, `contrasts`, `selection`, `next_baseline_measurement_id`, `listed` and `condition_changed_in_place`, plus the current comparison settings:

- `ordinal`: the entry's place in the round's list, from 1. It is the `KK` of its member ids, `m-rNN-final-KK-aMM`.
- `stability`: `stable` or `unstable`, as the final measurement was bound. `champion_side` is then `stored` or `fresh`.
- `stored_measurement_id`: under `stable`, the stored Champion measurement every contrast of the entry reads, which is the round's baseline; `null` under `unstable`.
- `members`: current measurement ids with `role` (`champion` or `finalist`) and `attempt_id` (`null` for the Champion). Under `unstable` the Champion comes first, `m-rNN-final-KK-champion`; under `stable` there is none. Until a member's completion is recorded, `complete`, `point`, `missing` and `critical` are `null`. A completed member has its score, missing-pair count and critical-failure flag; an incomplete finalist has `complete: false`, `point: null` and `missing` above zero. Rebound ids end in `@e<k>`.
- `contrasts`: each has `attempt_id`, `point`, `lower`, `level`, `pairing`, `accepted`, `reason`, `unavailable`, `exact`, `removed_pairs`, `removed_cases` and `condition_changed_in_place`. Reasons are `global_off`, `global_point_improved`, `global_bound_positive`, `critical_failure`, `final_incomplete`, `global_point_not_improved` or `global_bound_not_positive`. The two removal counts are `0` for a fresh comparison. `unavailable` is `null`, `bound_not_requested_in_observe_mode`, `final_incomplete` or `insufficient_repeats`.
- `selection`: shown on the round's last entry only, once the round selects. The reasons are `finalist_selected`, `no_accepted_finalist`, `global_point_not_improved` and `global_bound_not_positive`.
- `next_baseline_measurement_id`: the selected measurement once the round closes `promoted` or `retained`; otherwise `null`. Later scheduled refreshes appear in `settings.baseline_refresh.performed`.
- `listed`: rows of the round's latest `final_incomplete` listing whose measurement ids match this entry's current members. Each has `measurement_id`, `case_id`, `sample_id`, `retry_ordinal` and `owner`. The suspension lists at most 256 rows; stderr gives the total count. The listing stays after repair and is `[]` when no matching rows were listed.
- `condition_changed_in_place`: true when an accepted `continue` came after binding, or, under `stable`, when the finalist was measured in another condition epoch than the stored measurement.

Three top-level blocks are added:

```json
{
  "roles": {"editor": {"kind": "subprocess"}, "grouper": {"kind": "fields"}, "reflector": {"kind": "subprocess"}},
  "settings": {"baseline_refresh": {"count": 1, "every_rounds": 3, "performed": []}, "round_stop": "first_accepted", "stability": "stable"},
  "connection": {"attempts": 1, "binding_digest": "<64 hex characters>", "detail": null, "operation_id": "<32 hex characters>", "outcome": "compatible", "state": "compatible"}
}
```

- `roles`: the kind of the Grouper, the Reflector and the Editor in force, each as `{"kind": <name>}`.
- `settings`: the policy's `stability` and `round_stop` in force, and `baseline_refresh`. That is `"off"` when `baseline_refresh_after_rounds` is `0`, and otherwise `{every_rounds, count, performed}`: the period N, the number of rounds closed on the current stored Champion measurement, and every refresh bound, in journal order, as `{round, measurement_id, replaced_measurement_id, completed}`. A linked successor starts with `count: 0`. After a `rebaseline` that sets N to `0`, the earlier refreshes no longer show; the journal keeps them.
- `connection`: the latest connection preflight. `state` is `not_started` (no probe ran), `running` (a probe is open, or a failed probe's suspension is still owed), `compatible`, `interrupted` (a stop ended the probe) or `suspended` (the probe failed). A finished run keeps the state of its last probe, so an interrupted probe stays `interrupted`, and a linked `inherit` successor whose carried rounds never probed finishes `not_started`. `outcome` is `null`, `compatible`, `connection_incompatible`, `connection_preflight_unavailable`, `connection_preflight_inconclusive` or `interrupted`. `attempts` counts the preflights started in the run, interrupted ones included. `binding_digest` and `operation_id` name the binding and the probe's journal, `connection/<operation_id>/events.jsonl`. `detail` is `null` except after a failure: `{code, metric, manifest_path, observed_path}` for `connection_incompatible`, where `code` is one of `connection_event_missing`, `connection_field_missing`, `connection_field_empty`, `connection_field_type`, `connection_field_nonfinite`, `connection_primary_missing`, `connection_primary_type`, `connection_primary_nonfinite`, `connection_scorer_output_invalid` and `connection_description_secret`; and `{cause_code}` for the other two, where the cause is one closed code such as `timeout`, `process_failed`, `scorer_failed`, `target_failed` or `turn_budget`. `connection_description_secret` means the instance's `description` holds the value of a run secret: its `manifest_path` names where the manifest declares that secret, its `observed_path` is `target.description` and its cause is `null`. The detail never holds an observed value.

The `status` document's `actions` list names the final-measurement steps `RunFinalStage`, `BindFinalMember` and `CompleteFinalMember` where a family-13 status names `RunCohortStage`, `BindCohortMember` and `CompleteCohortMember`.

The top-level `caveats` list includes the [shared method caveats](running-campaigns.md#shared-method-caveats-in-15), with these additional fields and kinds:

- `champion_not_contemporaneous`, `{kind, round: null, attempt_id: null}`, is listed once for the whole campaign when it ran `stable`, after the two `observe` campaign items.
- `exact_contrast`, `{kind, round, attempt_id}`, appears for an Attempt whose look or final contrast was exact.
- `noise_not_observed`, `{kind, round, attempt_id}`, appears for an Attempt whose Parent showed no variation, so its measured tolerance was 0. It replaces `observe_calibration_degenerate`, which family 14 never reports.
- `stored_pairs_excluded`, `{kind, round, attempt_id, measurement_id, count}`, appears for each stored contrast that removed pairs: `measurement_id` is the stored Champion measurement and `count` the number of pairs removed. It comes last among an Attempt's items.
- `grouping_fallback` keeps its keys, with the family-14 reasons `invalid_output`, `input_too_large` and `turns_exhausted`.
- `components_changed_by_condition` draws its sides from `candidate`, `parent` and `final_finalist`, and the Champion member's item has `sides: ["final_champion"]`.

The scaffold's run lists `champion_not_contemporaneous`, then `focus_selection_bias`, `intervals_empirical` and `exact_contrast` for `r01-a01`. None of them changes a statistic, a decision or an exit code.

#### Independent comparison reports (family 15)

For `/16`, read `independent` in the result and status JSON rather than `/15`'s `confidence` block. `independent.settings` shows `mode` (`point`, `statistical`, `confidence` or `unfiltered`), `repeats`, `verification`, `fresh_parent`, `global_rule` and the noise assumptions. Its `method` is `f13-bootstrap/1` for `statistical`, `bounded-weighted-mean/1` for `confidence`, and `null` otherwise. `unit` is `case` or `group` for confidence and `null` otherwise. Optional `bound_fallback: point` appears only when declared. `evidence` distinguishes `snapshot_only`, `authenticated_publications` and `unavailable`; unknown evidence is not a failed Candidate score.

Each row in `independent.attempts` names `round` and `attempt_id`. `gate_comparison` is the local focus/causal decision; `global_comparison` is the full-train Candidate/Champion decision. Each is `null` until recorded. `comparison` keeps its existing meaning: the global comparison when present, otherwise the gate comparison. Read `gate_comparison` even after a finalist has a global result, so the later decision does not hide a local fallback. Each comparison's `reference_measurement_id` names its own Parent or Champion evidence; it is not necessarily the round's current baseline.

A comparison shows `outcome`, `reason`, `deciding_layer`, `coverage`, `reference`, `verification`, `method`, `unit`, `noise` and `scopes`. Each scope names its `rule` (`point`, `bound`, `bootstrap` or `off`), `status`, `point`, `threshold`, `strict`, `confidence`, `enclosure`, support counts and `unavailable` reason. Bound scopes use a lower-endpoint `enclosure`; bootstrap scopes instead add `lower`, `exact` and `pairing` (`exact` or `unpaired`, or `null` when unknown). Here pairing describes the provenance proof, whereas `exact: true` means no within-case contrast variation and lower equals point. Exact numbers use `rational-hex/1`; the human report's rounded approximations never decide acceptance.

Optional scope `fallback: {"reason":"focus_unattainable"}` or `fallback: {"reason":"causal_unconfirmable"}` labels a substituted point rule, for example `focus · point (fallback: focus_unattainable)`. This is weaker than passing the bound, not a response to poor Candidate scores. A comparison may retain its own optional `bound_fallback` declaration after a later policy change. A fresh measured-noise causal fallback appears only in the deciding result, once q is known; the binding still says `bound`. The other local scope and global rule remain independent.

Optional comparison `calibration` records Parent-measured q with `source` (`stored` or `fresh`), `measurement_id`, `prefix`, `retained` (causal case count), `method: f13-bootstrap/1`, `level` (0.995), `seed_input`, `allowance`, `degenerate` and `result_digest`. Stored calibration comes from the binding; fresh calibration comes from the deciding record. The causal threshold is `-q` when execution allowance e is zero, otherwise `e - q`, since allowance is reserved once. Empty causal scopes skip calibration. If calibration cannot be computed, the completed comparison gives `insufficient_calibration`, with no invented calibration or numeric causal threshold; q=0 is instead a valid calibration. A pending preflight's causal `status: uncalibrated` is shown as `calibration will be computed from fresh Parent at decision` for a fresh reference, or `Parent calibration unavailable` for a stored reference.

Optional comparison `caveats` lists `intervals_empirical` (empirical repeat intervals, not guaranteed coverage or generalization), `exact_contrast` (point decision), `noise_not_observed` (q=0 with no observed Parent variation) and `noise_allowance_empirical` (positive empirical q) when applicable. Too few usable observations give `comparison_unavailable` with `insufficient_repeats` or `insufficient_calibration`, not a statistical suspension or automatic extra calls. Scalar summaries, calibration metadata and decision references do not expose raw oracle data, private diagnostics, per-case scores or held-out test evidence. See the [gate reference](manifest-v2.md#independent-controls-manifest-16) for admission and fallback rules.

#### Linked runs

Every campaign that writes `campaign_finished` — `completed`, a threshold terminal, `infrastructure_failed`, `stopped` — reports `continuation: {"kind": "linked_run", "reason": <its reason>}` and can be continued by a successor campaign in a new run directory. After the JSON document, the command prints one line on stderr naming the two manifest keys that successor declares, in every mode, `--quiet` included (`--quiet` limits stdout only):

```text
evo tree evolve: continue: set linked_from to <absolute run dir> and seed_root to <absolute run dir>/<champion_path> (the target must allow trees under it)
```

`<absolute run dir>` is the canonical run directory, so it means the same in a manifest anywhere; `<champion_path>` is `status.campaign_outcome.champion_path`, the final Champion copy at `artifacts/champions/<version>`. A persistence or capacity fault wrote no finished record and is resumed after repair, not linked. Copy the current `/15` or `/16` manifest and use a new `--run-dir`. Both schemas require an object such as `"linked_from": {"run_dir": "<absolute run dir>", "baseline": "fresh"}`; choose `inherit` explicitly to carry eligible rows. A run-directory string alone is not accepted. The Target must allow the predecessor's Champion copy and the successor's artifacts.

The successor starts after the predecessor's highest opened round and measures its own baseline unless it declares `baseline: inherit`. It counts only its own indeterminate tries against thresholds; lineage counts remain reported separately. New `/15` links admit only finished family 14; `/16` links admit finished families 14 and 15. Removed predecessors get `lineage_family_unsupported`, unfinished current ones get hard `lineage_not_finished`, and an unverified Champion gets `lineage_champion_unverified`. A `/15` or `/16` `statistical` successor cannot link a family-14 predecessor that ran `observe` at creation or a rebase (`lineage_mode_unsupported`); a `/15` `observe` successor can follow either mode. See [the full link contract](manifest-v2.md#linkedfrom), including existing recorded family-13 predecessors and owed-import recovery.

A successor may differ from its predecessor in any comparable field — the target, the scorer, the environment, the corpus, the policy, the metric plan, the materialization, the artifact store policy, or a seed that does not scan to the predecessor's final champion. The link is not refused. Before the run starts, stderr carries one note and one `differs:` line per changed field, in the refusal's format — the predecessor's digest as `journaled`, the successor's as `observed`, digests and identifiers only:

```text
evo tree evolve: note: condition changed across the link
evo tree evolve: differs: identity.target.runtime_ref_digest  journaled <digest>  observed <digest>
```

The run then proceeds as any successor does, over its own baseline, and its result and status report the difference as a caveat, `caveats: [{"kind": "condition_changed", "from_run": <predecessor run_id>, "fields": [...]}]`; a successor with no difference reports `caveats: []`. `linked_from.lineage_caveats` carries the caveats of the chain before it, one item per kind (`kind`, `runs`, `fields`, `nearest_run`). A caveat changes no statistic, gate decision, outcome or exit code.

A resume normally reads only its own journal; an owed baseline copy is the exception below. Adding or removing `linked_from` after creation is a hard identity refusal. It prints `present` or `absent`, never the path:

```text
evo tree evolve: refused: run_identity_changed
evo tree evolve: differs: identity.linked_from  journaled present  observed absent
evo tree evolve: class: hard
evo tree evolve: A successor needs a new run directory; a campaign keeps the link it was created with, so linked_from cannot be added to or removed from a campaign that exists.
```

#### A carried baseline

A `/15` or `/16` successor that declares `"baseline": "inherit"` carries compatible predecessor Champion rows into its first-round baseline. Every compatible `(case_id, repeat, requested_seed)` with the primary metric and, under `exact`, the same dependency digest becomes `sample_imported` with its provenance; missing pairs are measured normally. `/16` also carries available bounded private diagnostics with explicit gaps ([independent controls](manifest-v2.md#independent-controls-manifest-16)). Before the run starts, stderr says how many pairs were carried and whether they came from another tree:

```text
evo tree evolve: note: baseline inherited: 118 pairs carried, 2 to measure
evo tree evolve: note: carried rows were measured on another seed tree
```

When no row is admissible — for example after an exact seed/dependency mismatch, or with no scored source row — the note is `nothing to inherit from the predecessor's baseline` and the baseline is measured in full, not refused. A nonempty carry reports `baseline_inherited` (`from_run`, `source_measurement_id`, `imported`, `scheduled`, `fields`, `origin_runs`) after condition-change caveats. `linked_from.baseline` reports `{mode, imported, scheduled, abandoned}` (null for `fresh`); measurements report `imported` beside `tries` and `excluded_pairs`. A caveat changes no statistic, decision, outcome or exit code. Both current schemas require an explicit `baseline` value when `linked_from` is declared; neither supplies a default.

If the predecessor's champion measurement cannot be read when the successor is created, the refusal is hard and names its own door, because nothing is declared yet:

```text
evo tree evolve: refused: baseline_source_unavailable at linked_from
evo tree evolve: class: hard
evo tree evolve: the predecessor's baseline cannot be read; declare "baseline": "fresh" in linked_from to measure it in full
```

The creation record declares the carried set (its `pairs_digest`) before any row is copied, and the rows are copied when the baseline measurement is born, atomically. A crash in between leaves an owed copy, which the next `evolve` completes by reading the predecessor again; this is the only case in which a resume reads the predecessor, so the resume manifest must still name it. If the predecessor has moved, changed or become unreadable by then, the refusal is authorizable:

```text
evo tree evolve: refused: baseline_import_unavailable
evo tree evolve: class: authorizable
evo tree evolve: accepting measures this run's baseline in full instead of carrying
evo tree evolve: the predecessor's rows
evo tree evolve: to accept this difference and continue, run:
  evo tree authorize <RUN_DIR> baseline_import_unavailable --digest <digest> --reason "<why>"
```

The digest binds the code, the run's `run_id` and the declared `pairs_digest`. The accepted decision is journaled as `authorization_applied` with `effect: fresh_baseline`, the baseline is born empty and measured in full, no `baseline_inherited` caveat is reported, and `linked_from.baseline` reads `abandoned: true` with the decision's `authorization_id`. While the copy is owed, a resume manifest whose `linked_from.run_dir` no longer passes the new-run path checks is refused `baseline_import_unavailable`, never `bad_path`; once the baseline is born, or once the campaign has finished or owes only its terminal, the run is self-contained and the path is not read. A pending `run_dir_unsafe_entry` finding is refused first, so the two may take one `authorize` each. A resume manifest must also declare the baseline mode the campaign was created with; otherwise the refusal is hard:

```text
evo tree evolve: refused: run_identity_changed
evo tree evolve: differs: identity.linked_from.baseline  journaled inherit  observed fresh
evo tree evolve: class: hard
evo tree evolve: A successor needs a new run directory; the manifest must declare the baseline mode the campaign was created with, so it cannot change on a campaign that exists.
```

#### A changed condition on an unfinished campaign

A comparable change on an unfinished campaign is refused `run_identity_changed` with `differs:` lines. Accept only an offered effect. When only `close` is offered, the refusal says:

```text
evo tree evolve: refused: run_identity_changed
evo tree evolve: differs: identity.scorer.digest  journaled <digest>  observed <digest>
evo tree evolve: class: authorizable
evo tree evolve: accepting closes this campaign as stopped(condition_changed);
evo tree evolve: continue it with a linked run that declares the new condition
evo tree evolve: to accept this difference, run:
  evo tree authorize <RUN_DIR> run_identity_changed --digest <digest> --reason "<why>"
```

When the accepted effect is `close`, the same `evolve` applies any hygiene decision, writes `authorization_applied` with `effect: close`, then `campaign_finished(stopped, condition_changed)`. It dispatches no new work and leaves measurements under their original identities. Stdout is the closed report, exit `3`; continue with a linked run declaring the new condition. An already owed terminal is completed rather than replaced. A finished run is hard-refused on identity drift. On unfinished current-family runs the offered effects may instead include `continue` or `rebaseline`, below.

A crash between the two records leaves a journal that owes its terminal. The next `evo tree evolve` on that run directory writes it before anything else is bound: after the manifest's schema check, the run directory's path safety and the journal lock, it replays the journal and, on a prefix ending in the applied close or terminal decision, appends the owed `campaign_finished` and returns the closed report. It builds no binding, runs no target preflight, compares no identity and needs no new decision, so a manifest whose target is down, or whose editor credentials are absent, still finishes it. A second writer holding the lock refuses `journal_busy` (exit `2`); a journal fault after the lock is a `persistence_failed` or `storage_capacity_exhausted` result (exit `1`).

#### In-place effects on family 7

This historical heading is retained for links; the rules here apply to current family-14/15 campaigns. An unfinished run's refusal prints every admitted effect, the default first without `--effect`, the others with it. A target switch can offer the following choices ([walkthrough](running-campaigns.md#change-the-provider-mid-campaign)):

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

| Changed comparable fields | Effects offered, default first |
|---|---|
| only `target`, `environment`, `materialization_strategy` | `continue`, `rebaseline`, `close` |
| any of `policy_digest`, `scorer`, `metric_plan_digest`, `metric_contract` (family 14), `corpus_digest`, `corpus_cases`, `evaluation_set_id` | `rebaseline`, `close` |
| `artifact_store`, with or without others | `close` |
| `role_visibility` (family 14, not a comparable field), with or without others | `close` |

`rebaseline` spends a round and is offered only when the rebuilt policy leaves this run another round, counted in rounds begun (an open round counts), not absolute round numbers. A policy below rounds already begun cannot rebaseline. Non-comparable differences normally do not change the set, except `role_visibility`, which offers only `close`. When `continue` is absent, one line says why:

```text
evo tree evolve: refused: run_identity_changed
evo tree evolve: differs: identity.scorer.digest  journaled b486ee51a4b50d66ff9b3b1433b2d9e6cd86492fee8faf191924eb256eb78b6c  observed e097ab2796b07c3f2c458da061ac5fb98c65bf3f4336d7b6786d0bbe80185273
evo tree evolve: class: authorizable
evo tree evolve: the scorer, metric, corpus or policy changed; existing scores cannot be compared on the new scale inside a round
evo tree evolve: to start the next round from a fresh baseline under the new condition, run:
  evo tree authorize run-3 --accept run_identity_changed:a1bbfc9f69e6fc8012c1083045fa8b390a8a1618c4a28b7088710fe3ed3f0701 --reason "<why>"
evo tree evolve: to close the campaign and continue with a linked run, run:
  evo tree authorize run-3 --accept run_identity_changed:a1bbfc9f69e6fc8012c1083045fa8b390a8a1618c4a28b7088710fe3ed3f0701 --effect close --reason "<why>"
```

When only `close` is left — no round remaining after a scale change, a policy below work spent, an artifact-policy change or an identity-rebase record too large to journal — the refusal uses the close text above and does not explain why the other effects are missing. That remains a preview limitation.

What an accepted decision writes, after an owed `alpha_consumed` and after any hygiene decision accepted in the same invocation, is one contiguous block:

- `continue`: `authorization_applied` with `effect: continue`, `identity_rebased` whose `changed_fields` include the comparable fields, then one `measurement_rebound` for every unfinished measurement the planner will still append to. Each re-bound measurement gets the old id with the suffix `@e<k>` (for example `m-r01-a01@e1`), is born with the old measurement's admissible scored rows as `sample_imported`, and measures only the rest. Complete measurements stay as they are and remain valid baselines. The open round goes on with its grouping, focus, diagnosis, gate scope and Candidates.
- `rebaseline`: `authorization_applied` with `effect: rebaseline`, `identity_rebased`, then, over an open round, `attempt_closed` with `abandoned` / `condition_changed` for every Attempt without an outcome and `round_closed` with outcome `condition_changed`. The next round begins with a fresh measurement of the champion, `m-r<NN>-champion@e<k>`. A round whose selection is already durable first finishes, promotion included, under the old condition, and the block follows at that boundary. Before round 1's baseline is bound, the baseline is simply measured under the new condition (and a declared `baseline: inherit` copy is not made).
- `close`: the close effect described above, ending this campaign without new work.

The same `evolve` then continues in the same process and reports its ordinary outcome. Closed rounds are not remeasured. Reports carry `condition_changes` (`seq`, `effect`, `fields`, `round`, `authorization_id`, `identity_digest`), an in-place caveat per change and `condition_epoch` per measurement. Only a changed owner's threshold window restarts: target for target/materialization changes, environment or scorer; never rollout. Without changes the list is empty and epoch is zero. Stdout redacts authorization ids; `inspect_campaign` shows them.

Five new refusals belong to this path, all hard, all before any write:

| Refusal | When | Door |
|---|---|---|
| `authorization_invalid at effect` | The decision's effect is not one the instance offers, for example `--effect continue` on a scorer change, or an explicit effect on a change that only rebases. | Write a decision with an offered effect. |
| `authorization_conflict at <id>,<id>` | More than one decision names this instance and they resolve to different effects. | Remove all but one from `authorizations/`. |
| `policy_contradicts_prefix at <key>` | A decision picks `rebaseline` over a policy below the run's spend; `differs: <key>  journaled <spent>  observed <new value>`. | Correct the value, or accept `close`. |
| `authorized_identity_changed` | A run stopped inside the block, and the manifest now yields another identity than the one the journaled decision accepted; three `differs:` lines, `identity_digest`, `producing_version` and `adaptation_digest`. | Restore the accepted manifest, and the package version that wrote the decision if it was upgraded, let the run finish the change, then present the new change on its own. |
| `rebound_source_corrupt at <measurement_id>` | A measurement frozen by the change no longer reads back as it was declared. Found after the run has written, it is instead the result `persistence_failed` with owner `measurement_journal` and this code (exit `1`). | Restore the run directory from backup. |

`continue`, `rebaseline` and a non-comparable `rebase` write the whole rebuilt identity and current case-family membership. Admission checks the record against the 1 MiB journal bound and forwarded secrets before writes. An inadmissible effect is withheld; if none remain or a decision selects one anyway, the hard refusal is `corpus_too_large` or `record_matches_secret`, naming the record location, never the value. Use smaller input, shorter non-secret labels or `close` when offered.

A conflict printed:

```text
evo tree evolve: refused: authorization_conflict at 6980064d38877001dd2e9901a263e61a6d67b4534723b0a8cb800ef015f983c9,9a481b9e33bd00e5fff8f919f87fec23301e4d89e7ededb97c79c167058e1806
evo tree evolve: class: hard
evo tree evolve: more than one decision names this refusal with different effects; remove all but one
```

A run that stops after `authorization_applied` completes the change on the next `evolve` without a new decision, provided the manifest still yields the accepted identity. Removed-family runs are not admitted to this path.

#### Suspension

Current campaigns write non-terminal `campaign_suspended` for infrastructure failures, after declared call and sample retries. Retryable target calls use `max_attempts`; turns use `turn_max_attempts`, with resending above one explicitly declared safe by the operator. Threshold, target contract, grouping, probe and unresponsive-role faults suspend, not score. Classified provider/unexplained role failures use the [resilience reasons](manifest-v2.md#reflector-and-editor-resilience-family-10). Component drift inside the reference epoch is `target_components_changed`; interrupted Champion gaps use `champion_incomplete`; infrastructure gaps in final measurements use `final_incomplete` and `final_rows`. Grouper and connection reasons are below.

The family-14 reasons and the cause each prints after the dash:

| Reason | Owner | Cause printed |
|---|---|---|
| `final_incomplete` | the listed pairs' owner | `the final measurement has pairs left without a score: an infrastructure-owned one on a finalist or any on the Champion; repair the cause and run the same command to resume`, then the number of unfinished final pairs and the first one with its owner |
| `grouper_provider_failed` | `grouper` | `the Grouper's provider failed after retries` |
| `grouper_failing` | `grouper` | `the Grouper failed for an unexplained reason or fell back for every failure twice in a row` |
| `connection_incompatible` | `connection` | `the connection probe found the Target or Scorer incompatible: <code> at <manifest_path> (observed <observed_path>); fix the Target or the Scorer, then run the same command (a changed Scorer or metric_contract is a comparable change: accept it with evo tree authorize)` |
| `connection_preflight_unavailable` | `connection` | `the connection probe could not reach a conclusion (<cause_code>); repair the cause and run the same command` |
| `connection_preflight_inconclusive` | `connection` | `the connection probe's rollout ended without a scoreable terminal (<cause_code>); repair the cause and run the same command` |

A connection sentence is filled from the status document's `connection.detail` ([final-measurement reports](#final-measurement-reports-family-14)). When that object does not name the same outcome, the line falls back to `decided by the record at sequence N`. The Grouper and connection suspensions are explained in [when the Grouper suspends](running-campaigns.md#when-the-grouper-suspends) and [when the connection preflight suspends](running-campaigns.md#when-the-connection-preflight-suspends).

`evo tree evolve` prints, after the JSON document and on stderr in every mode (`--quiet` included), the cause with its evidence and the command that continues the run:

```text
evo tree evolve: suspended: target_unstable — 9 of 30 tries in this window, threshold 25 %
evo tree evolve: continue: evo tree evolve --manifest <MANIFEST> --run-dir <RUN_DIR>
```

and exits `1`. The document's `terminal` is `suspended`, its `code` the reason, its `owner` the role to act on (`target`, `environment`, `scorer`, `rollout`, `editor`, `reflector`, `grouper` or `connection` in family 14, or `campaign` for grouping and the probe), and `suspended` the whole record, evidence included. A threshold names its window's counts and the bound it crossed; `champion_incomplete` names the Champion measurement that was cut and how many pairs it lacks; a fault-row suspension how many rows the stage holds and what the first one was coded; the remaining causes the journal record that decided them.

When a chat role's provider failure caused the suspension, one provider-attempt line comes between the two, and a chat role invocation that recovered from failed provider attempts prints its own line during the run ([read provider-attempt lines](running-campaigns.md#read-provider-attempt-lines)). A chat role without `api_key_env` gets a note before any role runs ([endpoints without credentials](manifest-v2.md#endpoints-without-credentials)). These lines go to stderr and the process log only.

For `champion_incomplete` the two lines read:

```text
evo tree evolve: suspended: champion_incomplete — the campaign was stopped before the Champion measurement m-r00-champion finished; 2 unfinished pairs, first c2/01
evo tree evolve: continue: evo tree evolve --manifest <MANIFEST> --run-dir <RUN_DIR>
```

`m-r00-champion` is the seed baseline; later baselines use round numbers, and a scheduled refresh is `m-rNN-refresh`. A baseline measured after `rebaseline` has an epoch suffix, such as `m-r01-champion@e1`.

Continuing uses the same command and run directory, without authorization, a new baseline or a linked run unless a bound input changed. A suspended grouping, probe or reflection reruns under a new operation id; an already abandoned Attempt stays abandoned. Each named fault row gets one repair try, so an unrepaired fault costs a bounded try per resume rather than closing the experiment. `champion_incomplete` and `final_incomplete` list unfinished pairs with their owners and give each one more try on plain resume. These recovery rules do not admit removed run families.

There is a second route to the same listing. A stop can also happen in a stage that decided a fault or threshold suspension. The campaign then suspends with that reason first. Repair what it names and resume. A later resume lists the unfinished Champion pairs as `champion_incomplete`, and a plain resume tries each one.

Thresholds count only the tries since the last suspension, so a resumed campaign does not re-cross the threshold that suspended it, and the report carries both that window and the campaign's totals ([run storage layout](run-storage-layout.md)). If the target or the scorer changed while the run was suspended, the resume meets the usual `run_identity_changed` refusal, and the operator chooses `continue`, `rebaseline` or `close` exactly as on a running campaign ([in-place effects](#in-place-effects-on-family-7)); accepting the change is also the repair of a `target_changed` suspension.

Current campaigns retry whole indeterminate samples under `campaign.sample_retries`, separately from target-call retries. Measurements report `tries` and `excluded_pairs`. Baseline exclusions, Candidate completeness and infrastructure repair have different rules; see [incomplete evidence](manifest-v2.md#incomplete-evidence) for `/15` and the independent `/16` controls.

If a public projection fails validation after the terminal is durable, the document has `code: projection_invalid`, `owner: campaign_projection`, `result: null` and a minimal `status` under the recorded run id. The terminal is unchanged. Exit `1` means Core could not produce the report; install a fixed Core. A suspended run resumes with the same command after the one `rebase` that any new release asks for. A finished run is not reopened by another release: `evo tree evolve` refuses it with `run_identity_changed`, so read its status with `evo tree watch --once` under the fixed Core. Stderr names `projection_invalid` and asks you to report the Core version.

The exit code reports the campaign's outcome, with exit `1` also covering a failed public projection:

| Code | Meaning |
|---|---|
| `0` | The campaign completed, whether or not a Candidate was promoted. |
| `1` | The current campaign suspended, its terminal was `storage_capacity_exhausted` or `persistence_failed`, or its code is `projection_invalid`. Repair the named cause and resume with the same command; a finished run with `projection_invalid` is read with `evo tree watch --once` under a fixed Core. |
| `2` | Invocation refused. Stdout is empty; stderr opens with `evo tree evolve: refused: <code>` or `<code> at <path>`, then difference, class and acceptance lines as applicable. A setting refusal adds a fixed `expected` sentence, never the invalid value. |
| `3` | The campaign was stopped. `SIGINT` and `SIGTERM` set a flag the campaign reads at its terminal barrier, so a stopped run still writes `campaign_finished` and still prints its document. |
| `4` | The campaign exhausted its budget. |

A Target instance whose `describe()` result holds the value of a run secret ends the invocation the same way: `persistence_failed` with owner `campaign_journal` and code `record_matches_secret` (exit `1`, one JSON document on stdout). The check runs on every instance after the connection probe's, Champion baseline instances included, when the instance is created and before any row runs on it. No `instance_attested` record is written for that instance; the measurement journal still records its creation and release. Stderr reads:

```text
evo tree evolve: instance_attested.description.<path> holds the value of <manifest_path>; the measurement journal is intact; remove the value from the target's describe() and run the same command again
```

Remove the value from the Target's `describe()` (for example report a hash) and run the same command again; removing the name from `allowed_env` instead changes the Scorer's identity and needs `evo tree authorize`.

A run directory the process cannot write into is reported the same way: `persistence_failed` (exit 1), one JSON document on stdout, not a refusal. The ordering is what decides it — an existing but empty run directory is a valid `--run-dir`, so admission and the campaign's own preflight both pass, and the failure happens later, when the campaign creates its journal and its lock and the write is denied. A refusal (exit 2) would say the invocation was wrong; what actually happened is that a valid run failed to persist.

Killing the process instead of signalling it leaves a resumable journal: run the same command again against the same run directory. The refusal codes are listed in [Manifest v2](manifest-v2.md#refusals); none of them carries a manifest value, a filesystem path or an environment value, the one exception being the `evo tree authorize` line offered for an authorizable refusal, which echoes back the run directory the operator typed.

The campaign creates an immutable Parent Snapshot and a run-owned Candidate workspace per Attempt. The declared Editor receives a redacted brief, workspace path, writable globs and deadline, never the raw oracle, private case fields or Target address. The Reflector investigates before the first Attempt, over a frozen Parent projection; it can read scorer verdicts and diagnostics, reflection metadata and declared documentation that the Editor does not receive. Its admitted diagnosis enters the Editor brief. The scorer defines metrics and the declared comparisons decide Promotion. Sealing refuses changes outside `writable_paths` or to dependency bytes. Scorer traces, role logs and documentation snapshots use their admitted owner-only output directories.

Current `/15` campaigns compare the affected focus cases against `focus.margin` and the causal cases against `causal.tolerance`, then apply `global` to a finalist's full-train final measurement ([stable-family gate](manifest-v2.md#what-family-14-measures)). `/16` separately declares point, statistical, confidence or unfiltered acceptance and reuse or fresh verification ([independent controls](manifest-v2.md#independent-controls-manifest-16)). Neither uses the retired layered-gate regression margin or alpha spending. A failed bound can reflect uncertainty, not proof of harm; interpret it under the declared mode and evidence scope.

### `evo tree conformance`

```bash
evo tree conformance --manifest v2-manifest.json
evo tree conformance --manifest v2-manifest.json --log-file conformance.log
```

`evo tree conformance` takes a full `/15` or `/16` campaign manifest, the same file as `evolve`. The whole document must validate, even though the service checks use only its target, seed and artifact policy. Every other campaign schema is refused `manifest_schema` with `expected evolution-v2-manifest/15 or /16`, before service contact. Workflow-less inputs use the library-only [`conformance_manifest`](manifest-v2.md#what-a-1-document-is-still-for), not a CLI file mode.

The command runs the generic adapter suite and HTTP wire suite against the running target and prints one `conformance/v2` JSON report to stdout. Its `leaked` list names probe instances the final sweep could not release; stderr repeats them as `conformance: leaked instances:`. Only this check's own instance ids can appear there; an unverifiable sweep uses `<unverified>`. Exit `0` means every check passed and no instance leaked; `1` means a failed check or leak; `2` means the manifest could not be loaded.

Conformance is not run implicitly by `evolve`; it is the operator's separate command, and the sensible thing to run once before the first campaign against a target.

`--log-file PATH` appends stdout and stderr from command entry, including a manifest refusal, with a header and closing `exit` line. A new file is `0600`; an existing file must already be a regular file owned by you, with one link and no group or other permission bits. Its mode is never changed. Keep this path outside every run directory. The copy is not redacted, because conformance has no run secret set, so read it before sharing it. A symlink, FIFO or failed private-file proof refuses with `conformance: log file log_file_unsafe`; another opening or descriptor error, such as a missing parent or permission denial, refuses with `conformance: log file log_file_unavailable`. Both exit `2` before reading the manifest. Argument errors raised before command entry, including a missing `--manifest` path, create no log. Later log failures disable the copy without changing the result. With the option, a broken terminal stream no longer ends conformance and its report remains in the log; without it, terminal handling stays as before.

### `evo tree authorize`

```bash
evo tree authorize ./runs/r1 run_identity_changed --digest 9d02… --reason "prompt v2"
evo tree authorize ./runs/r1 --accept run_dir_unsafe_entry:41c7… --accept run_identity_changed:9d02… --reason "moved; scorer v2"
evo tree authorize ./runs/r1 --accept run_identity_changed:9d02… --effect rebaseline --reason "provider B"
evo tree authorize ./runs/r1 --list
```

A refused `evo tree evolve` prints what differs, the refusal's class, and, for an authorizable refusal, the exact `authorize` command that accepts it. Run that command with a reason, then run `evolve` again. The refusal's stderr is this grammar, exit `2` and stdout empty:

```text
evo tree evolve: refused: <code>[ at <path>]
evo tree evolve: differs: <field>  journaled <digest>  observed <digest>
evo tree evolve: class: authorizable|hard
evo tree evolve: <what accepting does, for a close, an owed terminal or a lost baseline source>
evo tree evolve: to accept this difference and continue, run:
  evo tree authorize <RUN_DIR> <code> --digest <digest> --reason "<why>"
```

Effect lines describe a close (`accepting closes this campaign as stopped(condition_changed);`), an owed terminal (`accepting writes the terminal this campaign already owes;`) or a fresh baseline instead of an owed carry (`fresh_baseline`). A close or terminal names linked continuation and uses `to accept this difference, run:` because this campaign ends. Removed-family links are never offered for authorization.

Admission can report compatible findings together: run-directory/output-root hygiene, then a resume identity finding or an owed-baseline import failure. Every finding prints its own `refused:`, `differs:`, `class:` and effect lines. Other checks stop at the first finding; family refusal is hard and precedes authorization. When every finding is authorizable, one command accepts them:

```text
evo tree evolve: to accept these findings and continue, run:
  evo tree authorize <RUN_DIR> --accept <code>:<digest> --accept <code>:<digest> --reason "<why>"
```

When any of the findings is a close or an owed terminal, the lead line reads `to accept these findings, run:`, since the campaign ends.

`--effect {continue,rebaseline,close}` chooses among the effects offered for a current campaign's comparable change ([in-place effects](#in-place-effects-on-family-7)); it works with the single-code form and `--accept`. Without it, `run_identity_changed` records `effect: "default"`, resolved to the first offered effect. One explicit effect applies to every identity finding and no other finding; it is invalid with no identity finding or an unsupported value (`default` included). `--list` does not print the effect. Older decision files without this key retain their recorded close/rebase meaning, not a newly inferred default.

`--accept` is repeatable and writes one decision per item, with the shared reason, exactly as one `authorize` call per finding would, and prints one id per line; it stands alone, without a code, `--digest` or `--list`, and a malformed item or a hard code refuses `authorization_invalid at --accept` or `at code` before any decision is written. There is no form that accepts a finding not named on the command line. A refusal with one finding prints the single-code command as before. When a hard finding ends admission after authorizable ones were collected, the hard finding is printed first, the collected ones after it with their classes, no command is printed, and the hard finding's sentence ends the report: accepting the others would not admit the run. A decision supplied for only some of the collected findings admits nothing and writes nothing; the invocation refuses with the findings still pending.

There are as many `differs:` lines as fields changed and none for a code that reports no difference; there are two spaces before `journaled` and before `observed`, and a side that is absent prints the literal `None`. The one field compared by presence, `identity.linked_from`, prints `present` or `absent` on each side instead of a digest, and `identity.linked_from.baseline` prints the modes `fresh` or `inherit`. The last two lines appear only for an authorizable refusal; a hard one prints its own sentence instead — "the campaign is finished and there is nothing left to continue; start a linked run that declares the new condition" for a `run_identity_changed` against a finished campaign whose changed field is comparable and for one that reports no field at all, "the campaign is finished and there is nothing left to continue; start a linked run or a new run directory" for a refusal against a finished campaign whose differences are all non-comparable, whether it is a `run_identity_changed` or a `run_dir_unsafe_entry` — or nothing at all where the code has no sentence. A finished campaign takes no authorization at all, so even an otherwise authorizable code prints `class: hard` and no `authorize` line against one. A campaign that crossed its round threshold and never wrote its terminal is not that case: it still owes one record, so a non-comparable difference is authorizable and the `authorize` line is printed for it. The `authorize` line carries no `evo tree evolve:` prefix and is indented two spaces so it can be pasted as typed; it quotes the run directory as `--run-dir` carried it, which is the one place a refusal names a filesystem path, and it is the operator's own argument echoed back rather than a path the process discovered. Every line, the hint included, passes through the run's redaction.

`authorize` writes one file, `<run_dir>/authorizations/<id>.json`, and nothing else; it starts nothing, needs no manifest, and prints the decision id on stdout. The run directory need not exist yet: accepting a `run_dir_unsafe_entry` finding before the first campaign is the supported order, `evolve` refuses, `authorize` writes, `evolve` runs, and the decision directory is not counted as a foreign entry when the run directory is otherwise empty. `--list` prints one JSON object per decision (`id`, `code`, `digest`, `reason`, `issued_at`, `status` — `consumed` once a run in *this* run directory journaled it, `pending` otherwise), reports only, and is refused with `authorization_invalid at --list` if a code is given beside it. A decision belongs to the run directory it was written into: one consumed by a campaign elsewhere still lists as `pending` here. The run directory itself may be a symlink — that is a difference `evolve` admits under a decision — but `authorizations/` beneath it must be a real directory this user owns, or `authorize` refuses `authorization_invalid at authorizations` and writes nothing.

A decision matches one refusal instance by a digest of both sides: A→B never matches C→B. Its id is the SHA-256 of canonical JSON over `code`, `digest`, `reason`, `issued_at` and `effect` only when present. Journaled decisions are standing authority for that instance. Current campaigns offer identity changes, eligible hygiene findings and owed-import `baseline_import_unavailable` (`fresh_baseline`), not old-family admission or unfinished-predecessor links. Follow the printed effects; an authorization accepts differences and never introduces them.

Core reads `<run_dir>/authorizations/` through the canonical run root and one `O_DIRECTORY|O_NOFOLLOW` descriptor, not through CLI path enumeration. Directory/file authorizations are journaled with `source: file`; direct library `authorizations=` values use `source: caller`, which wins for a duplicate instance. Decision files are owner-only and keep the operator's reason as typed. A reason containing a forwarded secret is refused `record_matches_secret at authorizations/<id>` before journaling; malformed files cause `authorizations_invalid at <name>` until removed. `authorize` and `authorize --list` refuse removed-family run directories before writing any decision.

### Measure a tree on train or test

`evo tree evaluate` measures one frozen tree without running a Grouper, Reflector or Editor. It spends no promotion alpha and never changes a campaign's Champion. Start before and after measurements explicitly in different run directories.

```bash
evo tree evaluate --manifest evaluation.json --run-dir runs/before-test
```

Add `--progress` to `evo tree evaluate` to print the same live status lines as [`evo tree watch`](#evo-tree-watch) on stderr. It is off by default, has no manifest field and changes no evaluation evidence, role input or exit code. Progress drains before the final report; diagnostic notes print immediately on stderr, including before progress starts.

The CLI creates `<run_dir>/logs/evaluate-<UTC start>-<pid>.log` once the evaluation journal is open, its identity has been checked or created, and the evaluation is not already complete. It keeps the stdout report, stderr, progress and any unhandled traceback, redacted with the evaluation's own secret set even without `--progress`. A refusal before that point gets no log; a refusal afterwards, such as insufficient target capacity after birth, is recorded. Reopening a completed evaluation creates no new log. A broken or missing terminal stream no longer ends the logged invocation. The file and directory use `0600` and `0700`, with the same [bounds and failure handling](run-storage-layout.md#the-v2-run-directory) as campaign logs. A library call of `evaluate` creates no process log or process-wide capture.

Use `evolution-v2-evaluation/1`, with this complete shape. Paths resolve relative to the manifest. The scorer has the same protocol and binding rules as a campaign scorer.

```json
{
  "schema": "evolution-v2-evaluation/1",
  "snapshot": {"root": "seed"},
  "corpus": {"path": "corpus.jsonl"},
  "split": "test",
  "target": {"base_url": "http://127.0.0.1:8765", "seed_mode": "exact"},
  "artifacts": {"excludes": [], "dependency_paths": [], "writable_paths": ["skills/**"]},
  "environment": {"kind": "static", "input_text_key": "text"},
  "scorer": {
    "argv": ["python", "score.py"],
    "cwd": "scorer",
    "dependency_paths": ["scorer/score.py"],
    "allowed_env": [],
    "timeout_seconds": 30
  },
  "metric": {"primary": "score"},
  "repeats": 3,
  "random_seed": 42,
  "max_concurrency": 1,
  "max_scorer_concurrency": 1,
  "budget": {"max_turns": 4, "timeout_seconds": 120, "max_failed_turns": 1},
  "sample_retries": 0,
  "call_retries": {"max_attempts": 1, "turn_max_attempts": 1}
}
```

To measure a published campaign version, replace `snapshot` with `{"run_dir": "runs/campaign", "version": "v1"}`. Use the version from the campaign report; the source campaign is read only. Version `v0` is also valid, including when evolution made no promotion. A raw `snapshot.root` is copied into an immutable evaluation-owned artifact before measurement.

The corpus uses the [train/test contract](manifest-v2.md#train-and-test-corpora-manifest-12). `split` is required and its selected set must be nonempty. All operational fields shown above are required. Campaign budgets, role sections, promotion thresholds and `linked_from` are refused in an evaluation manifest.

Run the same command and directory to resume after an interruption. Completed scores are reused; unscored samples can receive one repair attempt on each explicit resume, plus the declared ordinary retry behavior for new samples. Changed bound inputs refuse resume. A completed run returns the verified report without contacting Target again. Exit `0` means complete measurement, including a low score; `1` means suspended or a persistence failure; `2` means admission refused. Incomplete reports show coverage and no complete-split metric.

From `2.0.0b7`, evaluation estimates free inodes at creation and incomplete resume, warns before store writes or new measurement when the estimate is low, and continues requested work. A completed evaluation skips estimation. Earlier b5/b6 releases refuse a low forecast with `storage_inodes_insufficient` and exit `2`; scripts using `2.0.0b7` must no longer rely on that pre-measurement stop. The warning is immediate even with `--progress`, uses human prose and adds no structured result. A missing notes sink or an ordinary exception from the inode-warning sink cannot block library work. Byte admission and actual storage faults retain their behavior: when a Core scorer-trace write fails, the CLI prints the pack file, then `evo tree evaluate: journal unavailable`, and exits `1`. Repair storage and repeat the same command; completed scores are kept. External programs can fail on exhaustion under their existing classifications, and recovery may repeat paid work. See [storage recovery](running-campaigns.md#when-the-disk-or-its-inodes-run-out).

```python
from pathlib import Path
from evolution.tree import inspect_evaluation

report = inspect_evaluation(Path("runs/before-test"), verify=True)
print(report["state"], report["completed_samples"], report["metrics"])
```

For asynchronous callers, import `evaluate` from `evolution.tree` and call `await evaluate(manifest_path, run_dir)` for the same operation. Evolution does not depend on Lab. Reports expose identities and equal-group scalar means, without a test promotion verdict or an automatic baseline import. Keep evaluation outputs outside role inputs; repeatedly inspecting test results can influence future authoring.

### `evo tree trace`

Export what each case did in a campaign or standalone evaluation without running it again. The unrelated `evo trace` group reads task/skill traces.

```bash
evo tree trace --run-dir DIR --out PATH [--manifest PATH] \
  [--measurement ID]... [--max-field-bytes N]
```

| Argument | Meaning |
|---|---|
| `--run-dir DIR` | Required campaign or evaluation directory. Reads campaign families 14 and 15 and standalone evaluation runs only. |
| `--out PATH` | Required new file outside the run directory, relative to the current directory. Created owner-only (`0600`); an existing destination is refused. `--out -` writes JSON lines to stdout instead. |
| `--manifest PATH` | Optional manifest of the run's kind: a full `/15` or `/16` campaign manifest, or `evolution-v2-evaluation/1`. Every other campaign schema is refused `manifest_schema` with `expected evolution-v2-manifest/15 or /16`. Checks the corpus digest and rebuilds the run kind's secret set from the current environment. |
| `--measurement ID` | Optional measurement filter, repeatable. Without it, exports all bound measurements in binding order. An unknown id is refused. |
| `--max-field-bytes N` | Maximum UTF-8 bytes per string value in a try, default `65536`, minimum `1`. Applies after redaction. |

Known limitation: `/16` campaign trace currently dispatches its reflection corpus rather than `/15`'s train-partition path. Do not assume `--manifest` authenticates a `/16` run containing held-out rows; inspect the actual corpus/request labels or export without the manifest. This does not change the campaign's train-only execution boundary.

The output is JSON lines: one header, then one `record: "try"` per execution whose rollout has a terminal record, including retries, repairs and imported rows. A rollout still in flight is counted in the summary and omitted; scoring without a terminal is exported as `unsettled`.

| Header key | Meaning |
|---|---|
| `record`, `schema` | `header` and `evolution-v2-trace/1`. |
| `run_id`, `kind`, `family` | Journaled run id; `campaign` or `evaluation`; campaign family or `null` for evaluation. |
| `producing_version`, `generated_by` | Run's producing version and exporter's package version. Producing version can be `null` before an evaluation measurement is bound. |
| `journal_heads` | Map of relative journal paths to the consumed valid-prefix byte counts. |
| `corpus` | `verified` with a matching manifest corpus, otherwise `absent`. |
| `redaction` | `campaign_secret_set`, `evaluation_secret_set`, or `none` without a manifest. |
| `max_field_bytes` | Applied string-value cap. The header and dictionary keys are not capped. |

| Try key | Meaning and provenance |
|---|---|
| `record` | `try`. |
| `measurement` | Journal-bound `id`, `role`, `round`, `attempt_id`, `candidate_digest`, `epoch`, `rebound_from`; optional `cohort_role` (`champion` or `finalist`). Epoch and origin come from binding history. Role can be `null` in old families; evaluation uses `evaluation`. |
| `case_id`, `sample_id`, `repeat` | Journal-bound case, sample and scheduled repeat. |
| `retry_ordinal`, `repair` | Journal-bound retry number and repair flag. An imported row has `retry_ordinal: null` and `repair: false`. |
| `imported` | `null` or the source row's `run_id`, `measurement_id`, `retry_ordinal`, `candidate_digest`. |
| `request` | `null` or `{source, check, public}`; checks below describe how the public input is bound. |
| `turns` | Scorer request's turns, each with `index`, `message`, `events`, `usage`, `latency_ms`, `outcome`, `reason`. Without a scorer trace, completed Target calls supply only journaled `index`, `usage` and target `latency_ms`; other fields are `null`. No completed call gives `[]`; suppressed or unavailable carried content gives `null`. |
| `oracle` | Scorer request's oracle or the verified corpus oracle; `null` when unavailable or suppressed. |
| `rollout` | Journal-bound `outcome`, disposition `class` (`score`, `indeterminate`, `exclude`) and `owner`, last failed Target call's `error_code`, envelope timestamps `started` and `finished`, and `target_calls` (call attempts included). Imported rows keep only outcome, class and owner; other fields are `null`. |
| `scoring` | `status` (`scored`, `failed`, `interrupted`, `unsettled`, `not_started`), `score_request_id`, `trace` (`present`, `missing`, `not_applicable`), journal-bound `metrics` and `critical_pass`, and `output`. |
| `scoring.output` | `null` if unavailable or unparsable; otherwise `check`, `metrics`, `verdicts`, `public_feedback`, `details`, `diagnostics` from scorer stdout, with absent fields `null`. |

Scorer requests and stdout are read through `evolution.tree.read_scorer_trace`, from either trace layout. `scoring.trace` is `missing` for every trace the reader cannot return: absent, unsafe, damaged or ambiguous.

Read the provenance before trusting content:

| Label | What was checked |
|---|---|
| `request.source: scorer_trace`, `request.check: authenticated` | With `--manifest`, the request agrees with the journaled scoring input digest and verified corpus case, including public input and oracle. This does not check the tree on disk. |
| `request.check: unauthenticated` | No corpus was supplied. Only the request's own input hash was checked; content is not authenticated against the run. |
| `request.check: mismatch` | A request check failed. The label survives, but `public`, `turns` and `oracle` are suppressed. |
| `request.source: corpus`, `request.check: corpus` | No scorer trace was available; public input and oracle came from the digest-checked corpus. |
| `scoring.output.check: unbound` | Parsed stdout metrics equal the journaled metrics. The journal does not bind verdicts, feedback, details or diagnostics. |
| `scoring.output.check: metrics_differ` | Parsed stdout metrics differ from the journaled metrics. The output remains visible with this warning. |

An in-run imported row follows its source scorer trace when available. A cross-run import keeps identity, metrics and provenance only; request, turns and oracle stay `null`. A try that failed before scoring has no stored message or agent events to export, even with a manifest.

Keep the export private: it can contain case content, oracle, private data, messages and scorer details. With `--manifest`, secret values in the current environment are replaced with `[REDACTED]` in every try string value and dictionary key. Campaign and evaluation secret sets use their own rules; unset declared variables cannot supply values to redact, and short values below the run kind's length floor are excluded. Without a manifest, nothing is redacted. This is best effort secret redaction, not removal of task data. Scorer traces themselves are currently written unredacted ([storage layout](run-storage-layout.md#the-v2-run-directory)).

After redaction, a string longer than the cap becomes `{truncated: true, bytes: N, sha256: HEX, head: TEXT}`. Length and hash describe the redacted UTF-8 string; `head` fits the cap at a character boundary.

A successful export prints a stderr summary like `evo tree trace: 18 tries written · 18 scored · 0 failed · 0 unavailable · 0 unverified · 0 in flight`. `failed` counts non-score rollouts or failed/interrupted scoring; `unavailable` counts null requests; `unverified` counts unauthenticated or mismatched requests. The line adds the number of declared secret variables unset when nonzero. The human line format and refusal code names are not promised parsing contracts.

| Exit code | Meaning |
|---|---|
| `0` | Export succeeded. |
| `1` | A journal could not be read consistently or the output could not be written. A partial output file is removed; stdout output cannot be retracted. |
| `2` | Invocation refused. No output file is left. |

Current refusals include `not_a_run_directory` (absent directory or no run journal), `run_dir_ambiguous` (both run journals), `campaign_family_unsupported`, `trace_out_exists`, `trace_out_inside_run_dir`, `manifest_kind_mismatch`, `corpus_digest_mismatch` and `measurement_unknown`. Manifest-loader refusals retain their own codes. Repair the named input and export again; this command neither resumes nor repairs a run.

### `evo tree watch`

Follow a campaign or standalone evaluation read only, without a manifest or a connection to Target.

For family 15, failure lines can add the leak case id, a closed Reflector category and schema path, or the last closed Editor protocol category and its `editor-logs/<attempt_id>.jsonl` path. The Editor reader skips bad lines and refuses symlinked logs. Missing, unreadable or uncategorized logs show only their path. No oracle pointer, rejected value or protocol message is printed. See [failure diagnostics](running-campaigns.md#watch-a-running-campaign).

```bash
evo tree watch --run-dir DIR [--once]
```

`--run-dir` is required and must already be a directory. If its journal is not born yet, the command prints a waiting line and polls once a second. Otherwise it prints the current snapshot, silently replays history and prints meaningful records appended after the start: rounds, stages, Attempts, measurement counts, decisions and suspensions. It reads only campaign families 14 and 15 and standalone evaluations. It takes no lock and writes nothing in the run directory. A removed family is refused as described under [evolve](#evo-tree-evolve), including with `--once`; a partial current-family creation line is not misclassified as an unversioned old run.

`--once` prints the snapshot and exits `0`, including for a finished or suspended run; a directory awaiting its journal prints the waiting line and exits `0`. Without `--once`, a run already finished at start returns its ending's code. A run suspended at start stays followed until it resumes and reaches a new ending. The follower stops at the first new finishing or suspending record, rather than treating an old suspension as a new ending.

Lines contain ids, counts and codes, with no task data: no case content, oracle, messages, agent output, scorer details or role text. Measurement counts print at most once per measurement every 10 seconds, plus at completion. After 60 seconds without a record, an idle line appears, then another every 300 seconds; idle does not end the watch. The lines are not a parsing contract. Use the trace record for per-try data and [`inspect_campaign(..., verify=True)`](running-campaigns.md#inspect-a-run-without-running-it-again) for verified campaign inspection.

| Exit code | Meaning |
|---|---|
| `0` | Campaign or evaluation completed, or `--once` returned its snapshot. |
| `1` | New suspension, infrastructure ending, or unreadable/rewritten journal. |
| `2` | Invocation refused, for example an absent directory, ambiguous run kind or unsupported family. |
| `3` | Campaign ended stopped. |
| `4` | Campaign exhausted its budget. |
| `130` | You interrupted the watch with Ctrl-C. |

### Pairing with the reference target

`evo-target serve` loads only complete `/15` and `/16` campaign manifests. Every other campaign schema is refused `manifest_schema` with `expected evolution-v2-manifest/15 or /16` before it starts serving. The library conformance builder does not add a file mode here.

A running target for either command can be the packaged reference target: `evo-target init ./demo` scaffolds a sample manifest and seed tree, and `evo-target serve ./demo/manifest.json` serves it on the manifest's loopback address until `SIGINT`. `evo-target` is a separate console script, not an `evo` subcommand; see [Reference target](reference-target.md).

The scaffolded manifest is the `/15` document `docs/reference-target.md` shows — one of the seven files `init` writes, alongside the seed tree, the corpus and the three sample programs — so `evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1` runs the sample campaign against the same target.

## Production Optimizer

```bash
evo evolve optimize SKILL \
  --train TASK [--train TASK]... \
  --validate TASK [--validate TASK]... \
  --solver-model MODEL \
  [--editor-model MODEL] [--rounds 1] [--trials 4] [--seed 0] \
  [--rewrite-mode edit-ops] [--manifest PATH] [--dir PATH] \
  [--version-decay|--no-version-decay] [--max-rewrite-attempts N] \
  [--api-base URL] [--solver-api-base URL] [--editor-api-base URL] \
  [--solver-temperature FLOAT] [--editor-temperature FLOAT] \
  [--solver-reasoning-effort off|low|medium|high|xhigh] \
  [--editor-reasoning-effort off|low|medium|high|xhigh] \
  [--permission safe|unsafe] [--max-tokens N] \
  [--timeout 300] [--pass-env NAME]... [--resume RUN_ID] \
  [--dry-run] [--json]
```

For example, route task attempts to local Qwen and reflection/rewrite to a separate local GPT-OSS server:

```bash
evo evolve optimize SKILL \
  --train train-a --train train-b \
  --validate val-a --validate val-b \
  --solver-model openai/Qwen/Qwen3.5-35B-A3B \
  --solver-api-base http://127.0.0.1:32035/v1 \
  --solver-temperature 0 --solver-reasoning-effort off \
  --editor-model openai/openai/gpt-oss-120b \
  --editor-api-base http://127.0.0.1:30004/v1 \
  --editor-temperature 1 --editor-reasoning-effort medium \
  --trials 4
```

The shared `--api-base` remains a compatibility fallback for either role with no role-specific endpoint. Loopback endpoints need no dummy API key, and the optimizer has no option that accepts a raw provider credential.

Training and validation lists are required, explicit, non-empty, unique, and disjoint. The solver backend must support explicit seed forwarding; Agent-CLI and GigaChat solver backends are therefore not valid optimizer solvers. With `--manifest`, training tasks must be in `train` and validation tasks in `val`. Every task must require the target skill exactly once; supporting required skills remain pinned throughout the run.

For each round, the optimizer:

1. runs a parent-only saturation preflight on validation with its own seeds;
2. collects and reflects only the current round's training traces;
3. creates a finalized candidate body;
4. reruns the candidate on training with the original collect seeds for diagnostic-only scalar measurements;
5. evaluates parent and candidate on validation with new shared seeds and counterbalanced arm order;
6. promotes only after at least four pairs, no per-task mean regression, and a conservative paired sign-flip gate.

Plans with fewer than four validation pairs fail before any model call. A fully saturated parent ends with `status=inconclusive` and asks for a validation task with headroom. A positive gain that the gate marks `underpowered` also ends `inconclusive`, keeps the parent active, preserves candidate evidence for inspection, and recommends increasing `--trials`.

Saturation, candidate-train diagnostics, and promotion validation do not create ordinary traces. Durable `run.json` state stores only scalar `seed`, `passed`, `total`, and `pass_rate` measurements plus decisions. Their prompts, responses, generated code, outputs, and evaluator feedback are neither persisted nor made available to reflection.

`--version-decay` is the default. It uses retained promotion count, not the numeric version label. `--no-version-decay` keeps the base budget constant. `--max-rewrite-attempts N` is the total editor-call count. Only `candidate_edit_budget_exceeded` starts another editor call.

`--dry-run` performs preflight and prints the call plan without model calls or writes. A real run checkpoints every phase under `.agents/skills/<skill>/.evolution/runs/<run-id>/run.json`. `--resume RUN_ID` continues interrupted work and rejects logical-run mismatches. A completed or inconclusive run returns its saved result. It checks task content, skill trees, bindings, model IDs, run settings, and rewrite policy. It also checks tokens, permissions, manifests, and passed environment names. Old partial runs without edit-brake identity require a fresh run. Dry-run output, checkpoints, and status expose sanitized endpoints only. They retain scheme, host, port, and path. They remove user information, queries, and fragments.

Inspect the latest or a named run without changing it:

```bash
evo evolve status SKILL [--run RUN_ID] [--json]
```

## Split And Onboarding Commands

```bash
evo split check MANIFEST [--evolution-tasks a,b] [--dev-tasks c,d] \
  [--reporting-tasks e,f]
evo split list MANIFEST [--scope skill_evolution|dev|reporting|all]

evo quickstart [-m MODEL] [--dir PATH] [--skill NAME] [--task NAME] \
  [--permission safe|unsafe] [--pass-env NAME]...
evo doctor [-m MODEL] [--api-base URL] [--task TASK] [--skill SKILL] \
  [--dir PATH] [--json]
evo init [PATH]
```
