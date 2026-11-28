# Install

[Русский](ru/install.md)

By the end of this page you will have Lab and Evolution Core installed in
a virtual environment and will have run a complete fixed evaluation on a
synthetic agent — no LLM, no network, no API keys, a few seconds of runtime.

If you have not read [How Lab works](concepts.md) yet, skim it first: this
page uses the words *Target Service*, *Bundle*, *version pair* and *conformance*.

## What you need

- **Linux or macOS.** Linux uses a child subreaper. macOS combines a process group, a process tree polled every 0.5 s and an environment marker. On macOS, a process that leaves the group, escapes the tree between polls and hides the environment marker can survive a stop. Windows is not supported.
- **Python 3.12 or 3.11** (Lab supports ≥ 3.11.4).
- **[uv](https://docs.astral.sh/uv/)** to build wheels and create the virtual
  environment, and **[just](https://just.systems/)** to run the setup recipes.
- **git**, with read access to the Lab repository.
- **Core.** The public checkout includes the pinned Core source and builds its wheel during setup.
- Network access once, so uv can fetch the build backend and dependencies
  from PyPI.

## macOS notes

The current pair supports macOS and Linux.

The checkout setup builds Core from the local repository. Use `shasum -a 256` in place of `sha256sum` on macOS.

The service socket path may contain at most 103 bytes on macOS (107 on Linux). `$TMPDIR` can have a long prefix: pass `--socket` with a shorter path in a private 0700 directory if needed. A symlinked ancestor such as `/tmp` is accepted; the state, scratch or home root itself must be a real directory.

## Why you build from source

Neither package is on PyPI yet. This checkout uses the **version pair**:

```text
evolution-lab 0.0.3.dev81  +  evo-skills 2.0.0b22 (exact)
```

See [Releases](../../lab/docs/RELEASES.md) for the version pair. Use the producing checkout and wheels for old runs. Use a fresh state directory for each pair:
Owner state is not migrated between pairs. The shared Lab-home project registry
has separate [additive upgrades](../../lab/docs/api/projects.md#the-lab-home).

Keep the two wheel files after installing. Lab asks for their paths later: a
Bundle records the exact wheel archives your environment was built from, and
Lab checks them again before every run.



## Install with just

From `lab/` in a clone of `sb-ai-lab/evolution`:

```sh
just setup      # core built from this repository
just demo       # the offline example from Step 5, checked and explained
```

`just setup` takes the pinned versions from `pyproject.toml`
(`evo-skills==…` in `dependencies`), so it never needs editing for a new pair.
It:

1. **builds the Core wheel** into `.lab/wheels/core-<commit>/`. Lab lives in
   `lab/` of the Core repository; the commit is the last one that changed
   anything outside `lab/`, so commits that change only Lab keep the same Core
   wheel. It exports that commit's files (without `lab/`) with `git archive`
   and builds the wheel there. Uncommitted Core changes are
   listed and left out; the working tree is not changed. If the Core version in
   that commit differs from Lab's pin, it stops and names both versions. If the
   wheel for the commit is already there, nothing is built.

   After a commit that changes Core, every recipe that runs Lab refuses
   until `just setup` installs Core of that commit. Use the complete checkout for setup.
2. **builds the Lab wheel** from this checkout, uncommitted changes included,
   into `.lab/wheels/`. The build is reproducible, so an unchanged checkout
   gives the same file and setup leaves it alone.
3. **creates `.lab/venv`** with Python 3.12 (`just python=3.11 setup` for
   3.11; a venv on another Python is recreated) and installs both wheels with
   the `[tree]`, `[service]` and `[mcp]` extras, reinstalling them every time so the
   installed files always match the wheels.

Run `just setup` again after every `git pull` or change to Lab's code.

A Core wheel built from the repository holds the same code as the release
wheel of that version only when Core sources have not changed since the
release tag; even then its archive SHA-256 differs (metadata and compression).
Lab works with either. Bundles record the archive hash, so Bundle digests made
with a self-built Core differ from those made with the release wheel.

`just demo` keeps its workspace in `.lab/demo/evaluation/` and
`just demo-campaign` runs the campaign example; [The demo](demo.md) explains
both. Other recipes: `just serve` starts the [local service](cli.md) on
`~/lab-state/<pair>` (for this pair `~/lab-state/dev81-b22`) with the
`--execution-wheel` arguments filled in; `just panel --open` starts the
[panel](panel.md) for it; `just lab <command>` runs any `lab` command from
`.lab/venv`; `just versions` shows the pinned pair, what is installed and the
wheel paths. `just` lists them all.

## Install by hand

The manual steps in [Install by hand](#install-by-hand) use one working directory with this layout:

```text
work/
  evolution/        a checkout of the public Evolution snapshot (Lab is in lab/)
  wheels/           the core wheel and the built Lab wheel
  lab-venv/         the virtual environment
```

The steps below do the same as `just setup` without just, in a layout you
choose, for example when Lab goes into an existing environment or the Core
wheel comes from a hand-off directory.

<a id="step-1-get-the-core-wheel"></a>

### Step 1: Get the Core wheel

Build Core from the public checkout:

```sh
mkdir work && cd work
git clone https://github.com/sb-ai-lab/evolution.git
mkdir wheels
uv build --wheel -o "$PWD/wheels" evolution
```

### Step 2: Build the Lab wheel

Use the same checkout for Lab. Continue from `work/`, beside the `wheels/` directory created in step 1:

```sh
cd evolution/lab
git rev-parse HEAD                   # record this source revision with the wheel
uv build --wheel -o ../../wheels       # → evolution_lab-0.0.3.dev81-py3-none-any.whl
cd ../..
sha256sum wheels/evolution_lab-0.0.3.dev81-py3-none-any.whl
```

A wheel rebuilt from the same commit can have a different archive hash. Keep
your own source revision and hash with the wheel; compare delivery artifacts
with [RELEASES](../../lab/docs/RELEASES.md) when available.

The Lab wheel contains only the Python package under
`src/evolution_lab/` (including the guides). Documentation-only commits outside
`src/` leave the package contents unchanged; edits to bundled guides change
them. Building and hashing a wheel does not test it: Steps 4 and 5 do.

### Step 3: Create the environment and install

```sh
uv venv -p 3.12 lab-venv
uv pip install --compile-bytecode -p lab-venv/bin/python \
  "evo-skills[tree] @ file://$PWD/wheels/evo_skills-2.0.0b22-py3-none-any.whl" \
  "$PWD/wheels/evolution_lab-0.0.3.dev81-py3-none-any.whl[service,mcp]"
```

Use `3.11` instead of `3.12` if you need to. The `file://` form needs
an absolute path, hence `$PWD`.

Lab dev78 added read-only MCP and the workspace connection. Lab dev79 adds actions such as starting a campaign and recording approval.
[Connect an assistant over MCP](mcp.md) describes the workspace and plugin paths,
and how to check the capabilities of your Lab build.

#### Extras: what to add and why

An *extra* is an optional group of dependencies you add in square brackets.

| Extra | Package | Adds | Install it when |
|---|---|---|---|
| `[tree]` | `evo-skills` | `httpx` and `openai` | Always, as above. Core's model-backed (`chat`) Editor role needs them. |
| `[service]` | `evolution-lab` | `fastapi`, `uvicorn` | You want to run the local service with `lab serve`, or a tool such as auto-evolve drives Lab through it. |
| `[gateway]` | `evolution-lab` | `fastapi`, `uvicorn` | You want the budget gateway, an HTTP proxy that caps LLM spend (see [Budget and cost control](budget.md)). |
| `[mcp]` | `evolution-lab` | the MCP SDK (`mcp>=2.3,<3`) | You want to connect an assistant to the running Lab service over MCP. See [MCP setup](mcp.md). |

`[service]` and `[gateway]` currently pull the same two packages, so either
one makes both features work. Name the one you mean anyway — the lists may
diverge later. To install both: `...whl[service,gateway]`.

Without `[service]`, the Python API and commands such as `lab guides` work
normally; only `lab serve` refuses to start, with the message
`The service requires the optional service dependencies.`

#### Why wheels, not an editable install

You might expect `pip install -e .` from the Lab checkout. Do not use it for
real work, for two reasons:

1. **Lab runs code in isolated subprocesses.** Core, your target and Lab's
   workers are started as `python -I ...`. The `-I` flag ignores
   `PYTHONPATH` and the current directory, so those processes always import
   the *installed* package, never your `src/` folder.
2. **Lab checks what is installed.** Before running anything, the Owner
   compares the installed Lab and Core files with the wheel archives recorded
   in the Bundle, byte for byte. An editable install has no installed copy of
   the wheel's files, so it does not match: Lab refuses with
   `environment_invalid` when you create the Bundle, and again before a run if
   the environment changed after that.

If you change Lab's own code, rebuild the wheel and reinstall it.

### Step 4: Check the installation

List the guides that ship inside the wheel:

```sh
lab-venv/bin/lab guides list
```

You should see eight lines, one per guide, starting with
`evolution-lab-budget`. `lab guides path` prints where they are
installed, so you can open them in an Editor.

Confirm the version pair:

```sh
lab-venv/bin/python -c "import importlib.metadata as m; print(m.version('evolution-lab'), m.version('evo-skills'))"
# 0.0.3.dev81 2.0.0b22
```

`lab` has no `--version` flag; the command above is the reliable check.

Optionally, look at the requirements catalogue — the checklist of what an
experiment package needs:

```sh
lab-venv/bin/lab guides requirements --json
```

On dev79 the catalogue reports `"catalogue_version": 13` and a `digest` starting with
`7249ffee`. Item titles are in Russian, because the auto-evolve app shows them
to its users; the guides explain each item in English.

### Step 5: Run the offline example

`examples/owner/evaluation.py` walks the whole fixed-evaluation path on a
stub agent that answers without any model. In a few seconds it:

1. writes the project skeleton (agent, adapter, corpus, scorer, launch file)
   into a new source directory, the same files `lab new-project` writes;
2. freezes it into a Bundle and prepares an evaluation of three cases with
   one sample each;
3. starts the agent as a target and runs conformance, which stops the target
   afterwards;
4. starts a fresh target, runs the fixed evaluation and reads the report;
5. stops the target and prints a JSON summary.

Run it from `work/`, beside the checkout:

```sh
lab-venv/bin/python evolution/lab/examples/owner/evaluation.py \
  --python lab-venv/bin/python \
  --lab-wheel wheels/evolution_lab-0.0.3.dev81-py3-none-any.whl \
  --core-wheel wheels/evo_skills-2.0.0b22-py3-none-any.whl \
  --state-directory lab-state-example \
  --source-directory example-source \
  --port 18765
```

Three rules for the arguments:

- `--python` must be the same interpreter that runs the script, and its path
  must not contain `..`: Lab compares it with the path the interpreter reports
  for itself, and `../lab-venv/bin/python` fails with `environment_invalid`.
  Run from `work/` as above, or pass an absolute path.
- `--state-directory` and `--source-directory` must **not exist yet**, and
  must be separate trees. Lab creates them.
- `--port` must be free on `127.0.0.1`; the target listens there.

#### What success looks like

The script prints a JSON summary. Check these fields:

| Field | Expected | What it tells you |
|---|---|---|
| `conformance.state` | `"succeeded"` | The target passed Core's protocol test suite. |
| `evaluation.coverage` | `{"expected": 3, "scored": 3}` | 3 cases × 1 seed were scheduled, and all 3 samples were scored. |
| `evaluation.report_state` | `"available"` | The measurement report can be read. |
| `final_target.state` | `"stopped"` | The target was shut down cleanly. |
| `blocker_count` | `0` | Nothing is left waiting for cleanup. |

You will also see `process`, `cleanup` and `persistence` blocks. Lab reports
these facts separately on purpose: a process can exit cleanly while cleanup is
still unconfirmed, and one never stands in for another.

The full reports, corpus and conversation records stay private inside the
state directory; the script prints only these summary facts.
[Run your first evaluation](first-evaluation.md) shows how to measure your own
agent with `lab new-project` and `lab evaluate`, and walks through the same
script step by step. To see what the example
writes and which files to look at first, read
[A tour of the examples](examples.md).

To run the example again, pick new directory names or delete the old ones
first.

> **New in 0.0.3.dev72.** From 0.0.3.dev72 the example builds its project from
> the project skeleton. To measure your own agent, create a project with
> `lab new-project`, connect your agent and run `lab evaluate`
> ([Run your first evaluation](first-evaluation.md#measure-your-own-agent)).
> On 0.0.3.dev71 and earlier the example uses Core's scaffold: two cases with two
> seeds, `{"expected": 4, "scored": 4}`.

### Troubleshooting

**`RuntimeError: state and source directories must both be fresh`**
One of the two directories already exists — often from an earlier run that
failed halfway. Choose new names or remove the old directories.

**`RuntimeError: target_start did not succeed: failed`**
The target could not start. The most common cause is a busy port: check with
`ss -ltn | grep 18765` and choose another `--port`. The failed run has already
created both directories, so use fresh names for the retry.

**`RuntimeError: run this example with the same interpreter passed as --python`**
You ran the script with one Python and passed another in `--python`. Use the
same `lab-venv/bin/python` for both.

**`PreparationError: A declared environment does not match its identity.`**
(code `environment_invalid`) The wheels you passed are not the ones installed
in the environment — for example a Core wheel of a different version, or a
Lab wheel from another build. Pass exactly the two wheels you installed, and
make sure the environment holds the matching pair. Lab never mixes pairs.
The same error appears when the `--python` path contains `..`, for example
`../lab-venv/bin/python`; run from `work/` or pass an absolute path.

**`PreparationError: A declared environment is unavailable.`**
(code `environment_unavailable`) The interpreter in `--python` is not a usable
virtual environment with Lab installed, for example the system Python.

**`PreparationError: The preparation request is invalid.`**
(code `invalid_request`) The request itself is malformed; for the example, a
typical cause is passing the same wheel for both `--lab-wheel` and
`--core-wheel`.

**`The service requires the optional service dependencies.`**
You ran `lab serve` without the `[service]` extra. Reinstall the Lab wheel
with `[service]`.

**Windows**
Use a Linux machine, VM or container. Lab supports Linux and macOS.

**Before upgrading**
Finish or stop every unfinished manifest `/15` run with its producing pair before
upgrading. Keep its wheels, environment and state for its reads and cleanup; start
new work with fresh state for the new pair. A run whose role interpreter lies inside
the seed must likewise finish or stop on its old pair: since b9 (b10 included) it is refused as
`program_inside_seed`. Move the declared interpreter outside the seed for a new run.
Lab offers no linked-run continuation (`linked_run_unsupported`) or state migration.

For dev58/b10 → dev59/b11, start with fresh state at `~/lab-state/dev59-b11`.
Keep old runs with their producing wheels and state for reading, continuation
and cleanup. There is no migration or runtime wrong-pair gate.

### Advanced: Core directly

If b10 stopped a `/16` campaign with
`CampaignInvariantViolated: invalid journal record` after `reflection_failed`
(fixed in Core b11), start a new campaign on dev59. Core itself can continue
that run after a producing-version rebase with `evo tree authorize`, followed
by the same `evo tree evolve` command; this is a Core capability, not a supported
Lab continuation path. See Core b11's
[CHANGELOG, Upgrade](https://github.com/sb-ai-lab/evolution/blob/main/CHANGELOG.md) for the frozen-Attempt exception.

**Old state directories from another version pair**
Use a fresh state directory for each version pair. Dev81/b22 uses `~/lab-state/dev81-b22` and Owner schema 14, which stores action approvals. Read, resume and clean up older runs with the wheels and state directories that produced them.

#### `lab evaluate` and `lab new-project`

New in 0.0.3.dev72. An error found before a run starts is printed as one
JSON line on stderr. It has an error `code`, the `file` and `field` it
concerns and, for a secret, its `role`. Exit code 2 means a problem in your
project; exit code 3 means a target that would not start, or a Lab or
environment failure. All codes: [Exit codes](first-evaluation.md#exit-codes).

**Exit 2, `secret_missing`**
`secret MY_AGENT_API_KEY (target role, launch.json) is not set in the environment`.
A name from `launch.json` `secret_names` (role `target`) or from
`evaluation-input.json` `scorer.allowed_env` (role `scorer`) has no value in
the shell that runs `lab evaluate`. `export` it and run again. Lab reads
secret values only from its own environment.

**Exit 2, `project_invalid` or `marker_not_allowed`**
A project file is missing, is not valid JSON, or has a field of the wrong
shape. The message names the file and the field. `"lab:auto"` is allowed only
in the nine fields where the skeleton has it; anywhere else it is
`marker_not_allowed`.

**Exit 2, `corpus_invalid`**
`corpus.jsonl: line 2: a required field is missing (missing_key)`. A corpus
line Core refuses: Lab runs Core's own corpus loader before anything
starts and says its verdict in words, with Core's code in parentheses:
not valid JSON, a missing or unknown field, a duplicate `case_id`, or a field
of the wrong type. `file` is the corpus and `field` starts with the line
number; an empty corpus names no line. The fields are in
[Cases and the corpus](evaluation-inputs.md#cases-and-the-corpus).

**Exit 2, `argument_invalid`**
`--wheel …: no such file` or `--python …: no such file`; `field` names the
argument. Check the path.

**Exit 2, `conflicting_values`**
Two fields that must be equal have different explicit values, for example
`launch.json:endpoint.base_url` and `evaluation-input.json:target.base_url`.
Make them equal, or set one or both back to `"lab:auto"`.

**Exit 2, `usage: lab evaluate needs the wheel files Lab and the core were installed from …`**
`--wheel` is missing. Pass the Lab wheel and the Core wheel you installed
from; `just evaluate DIR` passes them for you.

**Exit 2, `directory_not_empty` from `lab new-project`**
The target directory already has files. Nothing was written. Pick a new or
empty directory.

**Exit 3, `environment_invalid`**
`the Python environment check failed: A declared environment does not match its identity. evolution-lab: installed version … differs from the wheel's …`.
The wheels you passed are not the ones installed in the environment, for
example the Lab wheel of another version. Pass exactly the two wheels you
installed from. With `--python`, the same two wheels must be installed in that
environment too.

**Exit 3, `cleanup_failed`**
An earlier run of this project left something behind in its state (a target or
a task), and Lab could not clean it up. The message names the state directory
and the blocker; nothing new was started. Read the blocker as described in
[Cleanup, orphans and manual release](results.md#cleanup-orphans-and-manual-release).

**Exit 3, `target start not confirmed: target_unavailable`**
Your agent's process did not get ready. Right after you edit `adapter.py` or
`my_agent.py`, this usually means the adapter failed to import (a typo, a
package missing in the agent's environment) or `build` or `factory` raised.
A frequent cause is an environment variable your agent reads that
`launch.json` does not declare: the target process gets only the declared
`env` and secrets. The readout shows the traceback under "The agent's process
wrote (last lines of …/stderr.log)" and names that file in "Open first"; the
full log is `.lab/state-<pair>/scopes/<id>/stderr.log`. Fix the error and run
again.

**Exit 3, `unexpected_error`**
`lab evaluate stopped on an unexpected error: PermissionError: …`. Something
Lab does not check for in advance failed, for example a project directory it
cannot write. The JSON names the exception, the `state` directory and the
`run_dir` when they exist. Fix the cause (here: make the project writable)
and run again.

**Exit 3 after the run started**
The readout ends with `Measurement not accepted: <reason>` or, when the run
stopped before the evaluation, `Run stopped: <reason>`, and `exit code 3`, for
example when the result was not saved, cleanup was not confirmed, or
preparation refused the artifacts (`artifacts_invalid`) without naming a
corpus line. Open `evaluate.log` in the run directory: it lists the steps
and names the state directory. Lab picks a free port for each run; if you
wrote an explicit port into `endpoint.base_url`, check that it is free.

## Next

- [Connect your agent](connect-agent.md) — turn your own agent into a target
  (the next step on both paths in the [guide index](README.md))
- [Run your first evaluation](first-evaluation.md) — if you want the example
  above explained step by step first
- [A tour of the examples](examples.md) — the example files, and where to look
- [CLI and local service](cli.md) — the same steps from the command line

## Reference

For exact fields and error codes:

- [Owner guide](../../lab/src/evolution_lab/guides/owner/README.md) and [Owner API reference](../../lab/docs/api/owner-s4.md) — lifecycle, result shapes, refusals
- [Preparation guide](../../lab/src/evolution_lab/guides/preparation/SKILL.md) and [preparation API reference](../../lab/docs/api/preparation-s1.md) — environments, wheel archives, Bundle creation
- [Local service guide](../../lab/src/evolution_lab/guides/service/README.md) and [service API reference](../../lab/docs/api/service-s5.md)
- [Example README](../../lab/examples/owner/README.md) — the offline example's own notes
