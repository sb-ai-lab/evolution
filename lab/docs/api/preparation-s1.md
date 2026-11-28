# Preparation API: save inputs and run conditions

**Who this is for.** `evolution_lab.preparation` saves everything a future
campaign run needs (declared inputs, environments and launch conditions) as an
immutable Bundle and a PreparedExecution, without running anything. Read this
page when you integrate with the preparation API directly and need the exact
rules for inputs, argv, environments, summaries and error codes. If you are new
to Lab, start with the user guide page [Your first evaluation](../../../docs/guide/first-evaluation.md).

Use this synchronous API to copy declared inputs into an immutable **Bundle**
and save run conditions as a separate **PreparedExecution**. You can reopen
both later by digest. Preparation does not run the target or any campaign role;
the Owner starts the saved work explicitly.

The local current pair is `evolution-lab==0.0.3.dev55` with exact
`evo-skills==2.0.0b8`. Its scaffold writes campaign manifest
`evolution-v2-manifest/15` with the observe smoke profile; preparation accepts `/15` and
`/16` (family 15, opt-in). `linked_from` is refused as `linked_run_unsupported`, and core refuses a `campaign.promotion`
key as `unknown_key`. Use a fresh `~/lab-state/dev55-b8`; keep old runs and state
with their producing environments. Evaluation input is `lab-evaluation-input/1`, `/2` or `/3` (`/2` adds
an explicit fixed-evaluation budget; see below).
This page explains campaign preparation. For the evaluation path, use
[Run your first evaluation](../../../docs/guide/first-evaluation.md).
The API first shipped in S1; older version pairs and their manifests belong
to the [historical preparation example](../../examples/preparation_s1/README.md).

## Public surface

```python
from evolution_lab.preparation import (
    CampaignSource,
    EnvironmentInput,
    PathBinding,
    PreparationError,
    PreparationStore,
    SourceRoot,
    describe_environment,
)
```

The package also exports `EvaluationSource(input_path, launch_path)` for the
evaluation-only path, the summary types `BundleSummary`, `PreparedSummary` and
`EnvironmentDescriptor`, and `Failure`.

`EnvironmentInput(executable, wheel_paths, runtime_files=())` declares one
trusted Python interpreter, the exact saved wheel archives and, if present,
external runtime files. Only a POSIX venv with Python 3.11 or newer is
supported. The interpreter is run directly as
`<python> -I -S <stdlib-verifier>`; a shell, a console-script wrapper, a target
launcher and an arbitrary native executable are not supported here.

`describe_environment(input)` checks the installed contents of the venv against
the declared wheel archives and returns an immutable `EnvironmentDescriptor`.
Its `.digest` must be written into `launch.environment_ref`. With the older
launch declaration `lab-target-launch/1`, its `.executable` also goes into
`launch.command.executable`; with the current `lab-target-launch/2`, Lab starts
`<that environment's interpreter> -I -m evolution_lab.target.launch` itself and
no executable is written. `.to_payload()` returns the
descriptor without the digest and without the private paths of the wheel
receipts. Describing an environment does not install it, does not contact any
endpoint and does not prove that the target is ready.

`CampaignSource(manifest_path, launch_path)` points to a native campaign manifest `/15`
and a ready launch declaration. `SourceRoot(alias, source_path)` declares each
file or directory that may be copied. Native `/15` has no `extra_paths`, so every
additional input must be included in `roots`. The temporary template, receipts,
venv, wheel archives and runtime files should not be placed in copy roots.
`PathBinding(document, pointer, root_alias, relative_path)` is needed only to
explicitly bind a supported path field to an object that already exists in a
root; it does not add a new root and does not change the meaning of the
reference.

`PreparationStore(state_directory)` uses a local private store:

```python
environment = describe_environment(environment_input)
# The caller writes environment.digest into the launch (and, for launch /1, environment.executable).

store = PreparationStore(state_directory)
bundle = store.create_bundle(
    source=CampaignSource(manifest_path, launch_path),
    environments=(environment_input,),
    env={"MODE": "offline"},
    secret_names=("TARGET_TOKEN",),
    roots=(SourceRoot("project", project_root),),
    path_bindings=(),
)
prepared = store.prepare_campaign(bundle.digest)

reopened = PreparationStore(state_directory)
bundle_again = reopened.get_bundle(bundle.digest)
prepared_again = reopened.get_prepared(prepared.digest)
```

The first element of `environments`, the caller execution environment, must be
the interpreter of the current process (`sys.executable`), and it must be the
only caller execution environment. `create_bundle` independently checks it again
and compares the new descriptor digest with the launch; the result of an earlier
`describe_environment` call is not cached. A stale digest produces
`environment_invalid`; nothing is re-bound automatically.

The target may use the same environment: in that case `launch.environment_ref`
refers to the digest of the first element. For a separate target, first call
`describe_environment` for its `EnvironmentInput`, place that input after the
execution environment, and select its digest and executable in the launch. The
same executable must not be declared twice. All native command sections and the
launch use the pinned Python directly; executable scripts and all other
materials must be inside the declared roots.

For a native command `argv`, preparation accepts only this Python prefix: the separate
switches `-B`, `-E`, `-I`, `-O`, `-OO`, `-P`, `-s`, `-S`, `-u`, `-v`, `-vv`,
`-q`, `-b`, `-bb`, followed by a script path (the `--` separator is allowed), or
exactly `-c CODE`, or `-m dotted.module`. Unknown interpreter options, other
combinations of switches, attached `-cCODE`/`-mMODULE`, stdin, and a missing
entrypoint produce `unsupported_input`. A module name consists of non-empty
identifier components separated by dots; it is not a file path. Only a genuine
`CODE` in such a prefix remains opaque. After the entrypoint, all following
tokens are program arguments; `-c` and `-m` in that position do not cause the
next argument to be skipped.

The argv rule is deliberately conservative. The script path, and any separate
argument that points to an existing file or directory relative to the command
cwd, is an absolute path, or contains `/`, must resolve into the declared roots
and is rewritten relative to the copied cwd. Traversal and `\\` are rejected.
Attached path values in `--name=value` and `-xVALUE` are not supported,
including `--config=cfg.json` when the file exists, and `-C/path`/`-C./path`;
use the separate tokens `--config`, `cfg.json` instead. Option names that
contain a slash are not supported either. Literal values such as
`https://host/v1`, `provider/name` and `\\d+` should not be passed in argv: move
them into an explicitly set non-secret `env` or into a copied config file. Env
and a genuine Python `-c CODE` are not scanned for paths; hidden references
inside them are not carried over automatically.

`env` contains only explicitly passed non-secret strings; the ambient
environment is not loaded. `secret_names` contains only names, without values.
The names in the launch must be a subset of the full set passed to
`create_bundle`; S1 neither accepts nor stores secret values.

`create_bundle` copies and normalises only the declared roots, checks the
environments again, and publishes the Bundle. `prepare_campaign` reads only the
saved Bundle, checks the campaign/launch binding, rejects the reserved
`public.conformance=true`, expands the runtime-policy defaults, and publishes the
PreparedExecution. `prepare_campaign`, `get_bundle` and `get_prepared` do not run
the verifier and do not read the source checkout. Saved summaries are available
through attributes and through `.to_payload()`, which returns only the documented fields; they contain no env
values, corpus rows, argv, source receipts or source paths.

The Bundle summary uses `lab-bundle-summary/3`: `/2` plus nullable
`measurement_basis` ([definition and reading](service-s5.md#bundle-measurements)).
The `/2` warning fields are the same fields as in
`/1`, plus a closed list `warnings`
(`[{"code": str, "detail": {...}}, ...]`, empty by default). A warning never
causes a refusal and is not part of the Bundle digest: it is recomputed on every
`create_bundle`/`get_bundle` from the already saved descriptor and does not
change that descriptor. The only code today is `lab_version_mismatch`. For a
launch `lab-target-launch/2` (where the Lab launcher runs in the target's
environment), the `evolution-lab` pin of the execution environment
(`environments[0]`) is compared with the `evolution-lab` pin of the target
environment (the descriptor whose `digest` matches `launch.environment_ref`). If
they differ, or the target has no such pin, the warning is
`{"code": "lab_version_mismatch", "detail": {"expected": "...", "found": "..." | null}}`.
For a launch `/1`, `warnings` is always empty.

`PreparationError` carries a fixed, safe projection
`error.failure.to_payload()` with `code`, `stage` (`bundle`, `prepare` or
`read`) and `message`. This is a local API with no HTTP status mapping.

A campaign manifest with `linked_from` (a linked core run) is refused during
Bundle creation with the code `linked_run_unsupported`, before Lab copies inputs or checks their relocated paths; continuing a campaign is not a Lab capability
(PDR-0013). Through the service this is HTTP 422.

A `/15` manifest whose `grouper.kind` is `agent_cli` is refused during Bundle creation (and in a project's package
check) with the code `grouper_kind_unsupported`, stage `bundle`, HTTP 422 through the service, and the message "Lab
supports grouper kinds fields, command, chat (grouper.kind).". Lab supports the Grouper kinds `fields`, `command`
and `chat`; for `command` and `chat` it relocates the Grouper's paths to `/grouper/...`, redacts its secrets and
pins its program. The refusal comes before any copying or other side effect. Every Lab preparation path refuses
`agent_cli` the same way: the legacy `recipe.prepare_inputs` and the H1 `connect()` do too (with their own error
forms, see [legacy runner](legacy-runner.md) and [H1 client](client-h1.md)).

On exact dev55/b8, Lab delegates corpus placement to
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

## Fixed-evaluation budget

`prepare_evaluation` accepts `max_failed_turns`, `call_retries` and
`max_consecutive_unmeasured` as optional keyword arguments, including on a
campaign Bundle. New preparation resolves omitted/None values from evaluation
input defaults, then Lab's `retry` values: `max_failed_turns = max_turns`, call
retries `{max_attempts: 3, turn_max_attempts: 2}`, 3 sample retries, coverage 0.95,
and `max_consecutive_unmeasured: 3`. N must be a positive integer; `"off"`
disables only the consecutive-unmeasured stop. Input `/3` adds that default to
`/2`; `/1` and `/2` remain accepted with their old closed key sets.
Failure settings are not inherited from a campaign manifest. Explicit schedule
and metric arguments are still required for campaign-derived evaluation.

The resolved plan is visible in the prepared summary (default threshold 3 is
omitted); its turn budget is also echoed in the evaluation outcome.
Stored plans keep their old semantics and bytes. Effective threshold 3 is omitted
from canonical prepared payloads for old explicit-request compatibility; a
nondefault N or `"off"` changes the digest. All-explicit compatibility applies
to the same stored Bundle, not freshly captured environments or other pairs.
See [Evaluation budget](../../../docs/guide/evaluation-inputs.md#the-fixed-evaluation-budget)
and [measurement basis](service-s5.md#bundle-measurements).

<a id="limits-of-proof-and-relocation"></a>
## What stays outside the Bundle

The environment digest includes the absolute executable and runtime paths, the
platform, the verified wheel-member bytes and the recorded limitations. Wheel
archives, the venv/interpreter, runtime files and undeclared or transitive
dependencies remain external prerequisites; Lab does not copy or install them.
After the venv is moved, paths change or the environment is updated, the caller
describes it again and explicitly updates the launch. Old Bundles remain
readable together with their saved conditions.

For the caller execution environment, the origin check confirms the identity of
the current file on disk. It does not confirm Python code that is already loaded
in a long-lived process after an in-place upgrade. For a target descriptor, the
bytes of the installed members in the venv's purelib/platlib are confirmed; this
does not guarantee that the future target imports them rather than code selected
through `.pth`, `PYTHONPATH`, startup hooks or an editable external source.
The Owner performs its own runtime checks before starting work. Preparation
alone does not establish which code a later process will import.

The descriptor covers the declared environment, not readiness to run.
Native libraries, working endpoints and credentials remain the integration's
responsibility, as does approval for paid calls. To execute a saved plan, use
[Owner](../../src/evolution_lab/guides/owner/README.md); the earlier H1 client
has its own preparation API. Preparation itself installs nothing, starts no
work and provides no recovery or deletion/GC API.

A neutral, caller-oriented example is in
[`examples/preparation_s1`](../../examples/preparation_s1/README.md). It
requires a real integration's manifest and launch template, roots, venv and
saved wheels, and deliberately ships no agent, corpus, prompts, credentials or
target launcher.
