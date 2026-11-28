---
name: evolution-lab-preparation
description: Prepare and inspect a durable campaign or fixed-evaluation Bundle without running it.
---

# Durable campaign and evaluation preparation

Use `evolution_lab.preparation` to save an experiment's declared inputs and
run conditions. The result is an immutable Bundle and a PreparedExecution
that can be reopened by digest. This guide covers describe, bundle, prepare
and reopen; execution is a separate Owner action. Do not start a target,
campaign or evaluation, or call H1, as part of this preparation workflow.

Use the inputs supplied by the integration. Do not invent agent code, corpora,
prompts, credentials or launchers, or run readiness/conformance checks as part
of preparation. The [local service](../service/README.md) exposes the same
preparation operations through a Unix socket.

Owner SQLite schema13 records each start's prepared digest; schema12 added each start's Core refusal findings
and schema11 durable start initiation metadata. Schema10 added execution-policy confirmation and per-start policy history, retaining reflector-probe scopes and the separate per-start
Core-terminal columns introduced in v8. Connect upgrades a schema11 or schema12 state in place to schema13, which
an older Lab then refuses; earlier Owner schemas are refused with `unsupported_schema`, without migration. Deploy with a fresh private state for the producing pair. A matching
schema does not make another producing pair's state reusable. These instructions
name a prerequisite; they do not create state or upgrade old runs.

The exact Core archive SHA-256 is
`578f26a0c9c27f63a56fb049f90e4eb51c96e7180c429e333b25255247e0f6aa`.
Record the exact Lab wheel hash too; bundled guide edits change its bytes.

## Gather the inputs

The latest released pair is Lab `0.0.3.dev80` / exact core `2.0.0b21`.
Use a fresh state directory for the producing pair. This guide describes development source, including schemas newer than that release. See `docs/RELEASES.md` for delivery
history and source provenance. Keep
older state and runs with their producing wheels for reading, resume and cleanup.
Choose a new private state path, not the old dev58/b10 state; this guide creates no directory.
Owner SQLite schema13 upgrades a schema11 or schema12 state in place at connect, after which an older Lab
refuses it, and refuses earlier Owner schemas with `unsupported_schema`.
There is no other migration or folder copying.
A matching schema does not make another pair's state reusable. Do not copy an older
SQLite database, run tree or cleanup receipts into the new state directory. A campaign manifest with `linked_from` is refused
as `linked_run_unsupported` before copying, and a `/15` or `/16` manifest with an `agent_cli` Grouper as `grouper_kind_unsupported`.
A `/15` or `/16` manifest whose corpus lies inside `seed_root`, the scorer's `cwd` or a path an editor, reflector or
Grouper declares (`cwd`, `system_prompt`, `agent_bin`, `environment_docs`) is refused as `corpus_inside_role_workspace`
(core's own code, which core would otherwise raise only when the campaign starts) on every preparation path, before
copying. Keep the corpus in its own directory, for example
`lab/corpus/`, beside — not under — `agent_lab/` and the skills tree.

Custom subprocess Reflectors must accept `reflect_brief/5` for `/15`
and `reflect_brief/6` for `/16`. Editors receive `edit_brief/4` for `/15`
(or `edit_brief/5` with sequential attempt feedback), and `edit_brief/6`
for `/16` (or `edit_brief/7` with that feedback).
Keep older role programs and runs in their producing environments; do not
upgrade an old run in place.

On the development source with exact Core b22, Lab delegates corpus placement to
`evolution.tree.evolve.check_corpus_placement(manifest)` without `run_root` or
`environ`. It checks the core's declared role/grouping paths; it does not look
up an agent CLI on `PATH`. Runtime CLI resolution/checks remain core-owned.
A placement refusal keeps `corpus_inside_role_workspace`. Other helper failures
are not a successful check: H1 uses `manifest_invalid`/`io_error`; legacy recipe
uses `PreparationArtifactsInvalid`; Bundle uses `manifest_invalid`/`io_error`
with existing source-drift precedence; project package checks use
`manifest_invalid`/`unsupported_input`, with safe details. A missing corpus stays
with the existing corpus-validation stage. This exact-pin import is not a
promise of a stable beta facade.

For a campaign Bundle, ask the integration for:

- the native campaign manifest `/15` (written with the observe smoke profile) or the opt-in `/16` (family 15; see the campaign package guide), and a launch template (role programs read
  the Editor and Reflector briefs for that manifest version, listed above);
- a new explicit launch output path inside one declared source root;
- a new private pair-specific state directory shared later with Owner;
- each source root and any required explicit path bindings;
- the caller venv's `sys.executable`, exact retained wheel archives, and declared
  runtime files;
- an optional separate target venv with the same kinds of inputs;
- explicit nonsecret environment values and secret names only.

Keep the launch template, state directory, venv, wheel archives, runtime files,
and temporary metadata outside copied roots. Declare every additional copied input through `SourceRoot`. Never read ambient
environment values or request secret values for preparation.

## Describe the environments and write the launch file

Build `EnvironmentInput` for the current `sys.executable` and call
`describe_environment`. Put its input first in `environments`. If the target
uses another supported POSIX Python 3.11+ venv, describe that input separately
and append it; otherwise use the caller descriptor for both roles. Write the
selected target descriptor's exact `.digest` to `launch.environment_ref`. For
`lab-target-launch/2` and `/3`, Lab derives the interpreter from that same descriptor
itself and runs `evolution_lab.target.launch`; write the agent's entrypoint
into `launch.agent` (`module`, `attribute`, `import_root`) instead of a
`command`. `lab-target-launch/1` remains accepted for a custom launcher, where
the descriptor's `.executable` is written directly to
`launch.command.executable`. Use trusted Python directly, not a wrapper. Write
a new caller-selected launch file without overwriting the template or another
existing file.

## Declare commands and copied files

Keep native command argv within the supported Python grammar. Before a script,
`-c CODE`, or `-m dotted.module`, only separate `-B`, `-E`, `-I`, `-O`, `-OO`,
`-P`, `-s`, `-S`, `-u`, `-v`, `-vv`, `-q`, `-b`, `-bb` switches are supported;
`--` may precede a script path. Unknown interpreter options, combined forms
outside that list, attached `-cCODE`/`-mMODULE`, stdin, and missing entrypoints
are unsupported. Module components must be identifiers, not file paths. Only
the genuine interpreter `-c` body is opaque. After the entrypoint, every token
is an application argument; later `-c`/`-m` never hide the following operand.

Argv path detection is conservative: standalone absolute paths, slash-bearing
values, and existing cwd-relative files/directories must map into declared
roots. Traversal/backslashes are refused. Attached paths in `--name=value` or
`-xVALUE` (including `-C/path` and `-C./path`) and slash-bearing option names are
unsupported. Prefer separate `--config`, `cfg.json` file operands. Put literal
URLs, provider/model names, or backslash patterns in explicitly declared
nonsecret env or a copied config file instead. Do not scan or rewrite env/code
strings; any hidden paths there remain the integration's responsibility.

For command grouping, declare its executable, cwd, dependency paths, allowed
environment, and supported argv file operands like any other role command.
Treat grouping `config` as opaque finite data: do not interpret or rewrite
path-looking strings inside it. A declared dependency does not make a locator
inside `config` relocatable or promise filesystem isolation.

When `environment_docs` is present, expect a complete sorted inventory of every
regular UTF-8 file. Symlinks, special files, unreadable/non-UTF-8 entries, empty
trees, more than 4096 files, or more than 64 MiB of source bytes are refused.
`PreparedInputs.environment_docs_digest` is Lab provenance: SHA-256 of compact
UTF-8 JSON `{"schema":"lab-doc-inputs/1","files":[{"path":...,"sha256":...}]}`
with sorted keys and files sorted by path. It is neither the core snapshot digest
nor the older rendered-text digest, so do not compare those values.

Preserve an optional manifest `reflector.probe`; preparation never calls it.
Launch `/3` adds required `reflector_probe` and `turn` fields. An object in
`reflector_probe` tells Owner to launch and clean up the campaign probe; `null`
keeps the operator-runs-it-by-hand case. With no manifest probe, null is valid
and Owner makes no probe check. Declare the probe environment and copied script/
data paths in the Bundle just as for the target. Read the
[consistency table and launch example](../campaign-package/SKILL.md#reflector-probe)
before creating the Bundle; preparation does not verify matching data snapshots.
Owner cleanup never stops an operator-run server or deletes its external state.

## Save a campaign Bundle and plan

Create `PreparationStore(state_directory)`, call `create_bundle` with the launch
output, ordered environments, explicit roots/env/secret names/bindings, then
call `prepare_campaign(bundle.digest)`. Reopen with a new store and return only
the public `get_bundle(...).to_payload()` and `get_prepared(...).to_payload()`
summaries when a machine-readable result is needed. `create_bundle` independently
rechecks environments; do not reuse the describe result as proof or repair a
stale launch reference automatically.

## Save a fixed-evaluation plan

For evaluation-only preparation, use `EvaluationSource(input_path, launch_path)`.
`lab-evaluation-input/1`/`/2`/`/3` accept only the documented fields: seed root, artifact policy,
target binding, corpus path, static/scripted environment, subprocess scorer,
evaluation-set ID, metric plan and defaults. `/2` additionally allows
`defaults.max_failed_turns` (int ≥ 1) and `defaults.call_retries` (an object
`{max_attempts, turn_max_attempts}` or `"off"`); `null` for either key means
"not given", like an absent key. `/3` adds `defaults.max_consecutive_unmeasured`
(positive integer, `"off"`, or `null` = 3). Do not add
editor or reflector fields. The input, launch, seed, corpus, scorer
cwd/program/dependencies and any other file operands must be inside explicit
`SourceRoot` values. Wheel archives, the interpreter, state and runtime files
stay outside them. For a Bundle of kind `evaluation`, `launch.policy` must
point to the evaluation input itself before bundling, for example
`"policy": "evaluation-input.json"`; pointing to a campaign manifest is
`unsupported_input`, naming `launch.policy`. Lab generates a separate native
`/1` conformance manifest inside the Bundle and binds the saved launch to it.

Call `prepare_evaluation` with exactly one explicit selection:
`{"all": true}`, `{"count": N}`, or a nonempty ordered list
`{"case_ids": [...]}`. There is no random sampling. Ordered repeat seeds may
repeat: use integers for exact seed mode and `None` for none/unpaired mode.
Selection is a schedule, not a representativeness assertion. Evaluation-only
defaults can be overridden explicitly, including the fixed-evaluation budget
(`max_failed_turns`, `call_retries`; `None` means "use the document's default,
or Lab's current `retry` fallback if the document has none"):

```python
prepared = store.prepare_evaluation(
    bundle.digest,
    selection={"case_ids": ["case-b", "case-a"]},
    repeat_seeds=[17, 29],
    evaluation_set_id="fixed-v1",
    max_turns=4,
    timeout_seconds=30,
    metric_plan={"primary": "score"},
    max_failed_turns=3,
    call_retries={"max_attempts": 4, "turn_max_attempts": 1},
)
```

All three input majors accept evaluation defaults `sample_retries` (null or
integer 0–8), `min_coverage` (finite, >0 and <=1), `concurrency` and
`scorer_concurrency` (integers >=1). Named Python arguments override the defaults.
New preparation resolves omitted/default-null failure settings to `retry`:
3 sample retries, coverage 0.95, `max_failed_turns` equal to resolved `max_turns`,
call retries `{max_attempts: 3, turn_max_attempts: 2}`, and 3 consecutive unmeasured
samples before stopping. `max_consecutive_unmeasured="off"` disables only that stop;
a positive integer sets its threshold. Concurrency defaults to 1 for both kinds;
`concurrency` cannot exceed `limits.max_concurrent_sessions`, and samples use
one target instance. See [Evaluation budget](../evaluation-inputs/SKILL.md#evaluation-budget).

Stored plans retain their original defaults, bytes and digests, including old
absent/null retry fields. Threshold 3 is the effective property but is omitted
from the canonical payload for old-request compatibility; nondefault N or `"off"`
is stored and changes the digest. On the same stored Bundle an all-explicit old
request keeps its old prepared bytes; new environment captures can change Bundle
digests. There is no migration between producing pairs/states.

A campaign Bundle still requires explicit `selection`, `repeat_seeds`,
`evaluation_set_id`, `max_turns`, `timeout_seconds` and `metric_plan`
(`invalid_request` for missing values). Failure settings are now optional and
resolve to Lab's defaults; the campaign gate, looks, repeats, selection, metric
and failure settings are never inherited. Evaluation reuses the declared target,
seed, corpus, environment and scorer. Changing the resolved plan changes the
PreparedExecution digest.

<a id="measurement-basis"></a>
## Measurement basis

`get_bundle(...).measurement_basis` and its `lab-bundle-summary/3` payload expose
a nullable SHA-256 of the saved fixed-evaluation inputs. For campaign Bundles,
the basis includes environment/scorer, seed, target, artifacts policy and corpus,
the saved file inventory with file kinds/executable bits, launch settings except
`limits.max_instances` and `limits.max_concurrent_sessions`, environment descriptors,
`env`, secret names, source kind and conformance entrypoint. Manifest and launch
files enter through their projections; other files are included conservatively,
even a campaign-only role brief. Evaluation/probe inputs also enter as saved
input documents; equal bases across source kinds are not promised.

Campaign scale, retries, role models and gate settings alone do not change this
basis. Changing scorer bytes, seed, corpus, artifacts policy or launch input/output
limits does. A basis covers Bundle inputs, not plan equality or result quality.
Unavailable/invalid saved inputs have `measurement_basis: null`; no reusable
basis is inferred. Reading does not capture an environment or create a Bundle.
For plan conditions and outcomes use [measurements for a Bundle](../service/README.md#bundle-measurements).

Evaluation-only check and runtime remain Owner responsibilities. Preparation
does not call the scorer, target or conformance runner, and does not need editor
or reflector settings or secrets. Reopening saved Bundle and PreparedExecution
summaries reads the saved bytes without the source project or verifier calls.

## What successful preparation establishes

Describe/create invoke only the isolated `-I -S` stdlib verifier. Prepare/get
read saved data only. A saved preparation is not runtime evidence or permission
to run. Wheel archives, interpreter/venv, runtime files, native libraries, and
undeclared/transitive dependencies remain external prerequisites.

When reporting results, explain two limits. The verifier checks files on
disk, not code already loaded into a process before an in-place upgrade.
Matching target wheel files also does not establish which modules a future
process will import through `.pth`, `PYTHONPATH`, startup hooks or an editable
external source. Owner performs its own
runtime rechecks before dispatch. Explicit runtime conformance and fixed
evaluation belong to the Owner S3 API and are never implied by successful
preparation. New conformance requires launch declaration
`{"kind":"core-session-public/1"}`; the historical
`core-a5-session-public/1` token remains readable but does not authorize it.

A `/15` or `/16` manifest without the gate keys is refused with `manifest_invalid` carrying core's text. Lab never repairs it.

A `/15` or `/16` manifest whose `grouper.kind` is `agent_cli` is refused with `grouper_kind_unsupported` (stage `bundle`, HTTP 422: "Lab supports grouper kinds fields, command, chat (grouper.kind)."), before any side effect. Lab supports the kinds `fields`, `command` and `chat`.
