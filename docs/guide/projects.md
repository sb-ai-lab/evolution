# Projects and scenarios

[Русский](ru/projects.md)

**Goal:** register an agent as a Lab project and walk it through the two
built-in scenarios — `connect` (freeze it, prove it runs, measure a
baseline) and `evolve` (let Core try to improve it) — using the free
demo agent that needs no model, no network and no git. **You are done
when** the demo project has passed `connect` and run one `evolve` cycle,
and you can see both in `lab project status`.

> **First measurement of your own agent?** Start with the offline commands
> instead (new in 0.0.3.dev72): `lab new-project DIR` writes a project
> skeleton and `lab evaluate DIR` measures it, without the service or the
> project registry. See
> [Measure your own agent](first-evaluation.md#measure-your-own-agent).
> `lab project` does not read the skeleton yet; this page is for the project
> registry and its scenarios.

The page has two parts. **[Steps](#steps)** is the checklist to follow.
**[Reference material](#reference-material)** below it explains the Lab
home, the socket, the demo's deterministic failure and what Lab never does
to your working copy.

Use a fresh state directory for this pair, such as `~/lab-state/dev81-b22`. Owner schema 14 stores action approvals. Read, resume and clean up older operations with their producing wheels.

New dev60 operation facts bind the ordered environment declaration and the
bytes of its declared wheels/runtime files, as well as the source inputs.
Changing these inputs makes check/conformance/baseline facts stale. Earlier
bindings keep their recorded sources and digest meaning; Lab does not expand
old evidence to cover new inputs.

## Before you start

- Your pair is installed ([Install](install.md)): `evolution-lab 0.0.3.dev81`
  with `evo-skills 2.0.0b22`, `lab` on your `PATH`.
- Start the service with a state directory and the two execution wheels.
  Leave out `--socket`: without it the service listens on
  `<home>/service.sock` ([Reference material](#reference-material)), so
  every client command below needs no `--socket` either.

  ```bash
  mkdir -m 700 -p ~/lab-state/dev81-b22
  lab serve --state ~/lab-state/dev81-b22 \
    --execution-wheel "$(realpath ../wheels)/evolution_lab-0.0.3.dev81-py3-none-any.whl" \
    --execution-wheel "$(realpath ../wheels)/evo_skills-2.0.0b22-py3-none-any.whl"
  ```

  A new pair (a new Lab or Core version) needs a new, empty state
  directory: do not reuse the previous pair's state (ADR-0017; Lab does not
  migrate state).

- In a second terminal, activate the same environment
  (`source .lab/venv/bin/activate`). If you gave `--home` (or set
  `LAB_HOME`) to `lab serve`, pass the same one to every `lab` command
  below.
- You know Bundles, targets and operations
  ([How Lab works](concepts.md)); a project does not change what those are,
  it only records which ones belong to which agent.

## Steps

1. [Create the demo](#step-1-create-the-demo)
2. [Read the status](#step-2-read-the-status)
3. [Confirm the spec](#step-3-confirm-the-spec)
4. [Package, check and check conformance](#step-4-package-check-and-check-conformance)
5. [Measure a baseline](#step-5-measure-a-baseline)
6. [Evolve one cycle](#step-6-evolve-one-cycle)
7. [Your own project](#step-7-your-own-project)
8. [Work with an assistant](#step-8-work-with-an-assistant)

### Step 1. Create the demo

**Do:** ask the service for the free demo project. It needs a configured
service environment (the `--execution-wheel` flags above); without one the
call answers `409 service_environment_unconfigured`.

```bash
lab --json project demo demo
```

The project's display name is `Demo (demo)` (`Demo (<id>)`), so several demos stay
distinguishable in lists. This writes the working copy to `<home>/clones/demo/` — no git, no model —
with a `SPEC.md` and a `spec.answers.json`, a scaffolded evaluation package under `lab/`, and a synthetic
agent under `agent/` that echoes its skill file's first line back at you.

**Done when:** `lab project show demo` reports `"state": "ready"`.

```bash
lab --json project show demo
```

### Step 2. Read the status

**Do:** ask what the `connect`, `evolve` and `restructure` scenarios need from this project
right now.

```bash
lab --json project status demo
```

**Done when:** you see four scenarios: `connect`, `create-skill` (creating one skill in an implementation; it starts with `task`), `evolve` and `restructure` (the last unavailable until the implementation is a port, `lab project port`), each with a
`steps` (or, for `evolve`, `next_cycle.steps`) list — some `ready`, the rest
`blocked` on what comes before them.

Every artifact a step reports as done (`sources[]`, for example `{"artifact": "SPEC.md",
"digest": "sha256:…"}`) carries the digest of the bytes Lab looked at. To confirm exactly
that file, send the digest with `lab project confirm … --digest sha256:…`: if the file
was replaced in between, the confirmation is refused (`digest_mismatch`). When a step is
blocked with `artifact_invalid`, each `invalid` record names its file (`artifact`) next to the
JSON Pointer inside it (`path`) and the message.

### Step 3. Confirm the spec

**Do:** the `connect` scenario's first human step is reading
`SPEC.md` and saying so. The demo's copy is at
`<home>/clones/demo/lab/SPEC.md`; it ships `spec.answers.json` next to it, the answers
file the `spec` step checks against its schema. Wait until `lab project status demo` shows `spec` as `done`
before you confirm: a confirmation made before the answers file exists goes stale at once.

```bash
lab --json project confirm demo --implementation demo \
  --claim spec.confirmed --path SPEC.md --author <you> \
  --link catalogue:requirements --link file:spec.answers.json
```

**Done when:** `lab project facts demo` shows `fact spec.confirmed
holds=yes`.

### Step 4. Package, check and check conformance

**Do:** freeze the project into a Bundle, connect it to this Owner
generation, start a Target Service from it, and run both readiness checks.
`project target` has no `--wait`: it returns a task reference right away,
and you poll `task status` until it ends. Keep `BUNDLE_DIGEST`: you need it
again in every later step, and a fresh `TARGET`/`GENERATION` pair after
every target start.

```bash
lab --json project bundle demo
# use the response's bundle.digest as BUNDLE_DIGEST

lab --json connect BUNDLE_DIGEST
# use the response's id as CONNECTION

lab --json project target demo --bundle BUNDLE_DIGEST
# use the response's task.id and task.owner_generation
lab --json task status TASK_ID --generation GENERATION
# repeat until state is "succeeded"; then use ref.id as TARGET, owner_generation as GENERATION

lab --json check CONNECTION --target TARGET --generation GENERATION --wait
lab --json conformance CONNECTION --target TARGET --generation GENERATION --wait
```

Conformance always stops its target when it finishes, so the target you
just used cannot be reused for the baseline.

**Done when:** both `check` and `conformance` answer `"state":
"succeeded"`, and `lab project facts demo` shows `check.passed` and
`conformance.passed` with `holds=yes`.

### Step 5. Measure a baseline

**Do:** start a fresh target on the same Bundle (conformance stopped the
last one), then prepare and run a fixed evaluation. The demo's generated
`evaluation-request.json` ships with `"bundle_digest": null` — `project
prepare` refuses `null` with `invalid_request`, so fill it in first (a copy
keeps the working copy's own file untouched):

```bash
lab --json project target demo --bundle BUNDLE_DIGEST
lab --json task status TASK_ID --generation GENERATION
# a new TARGET / GENERATION

jq '.bundle_digest = "BUNDLE_DIGEST"' \
  <home>/clones/demo/lab/evaluation-request.json > /tmp/evaluation.json

lab --json project prepare demo --request /tmp/evaluation.json
# use the response's prepared.digest as PREPARED_DIGEST

lab --json project start demo --prepared PREPARED_DIGEST --target TARGET
lab --json status OPERATION
```

**Done when:** `status` shows `"control_state": "finished"`, and `lab
project facts demo` shows `baseline.measured holds=yes` and `connected
holds=yes` — the `connect` scenario is `produced`.

### Step 6. Evolve one cycle

**Do:** write a campaign request over the same Bundle, prepare it, start a
fresh target, run the campaign to the end, and export whatever it changed:

```bash
cat > /tmp/campaign.json <<'JSON'
{"kind": "campaign", "bundle_digest": "BUNDLE_DIGEST",
 "stop_grace_seconds": null, "cleanup_wait_seconds": null}
JSON

lab --json project prepare demo --request /tmp/campaign.json
# use the response's prepared.digest as CAMPAIGN_DIGEST

lab --json project target demo --bundle BUNDLE_DIGEST
lab --json task status TASK_ID --generation GENERATION
# a new TARGET / GENERATION

lab --json project start demo --prepared CAMPAIGN_DIGEST --target TARGET
lab --json status OPERATION

lab --json export OPERATION --wait
```

The campaign request is a closed form: all four keys are required and `null`
means "Lab's default" for the two timeouts. Keep the request file **outside the
working copy** (here `/tmp`; for an assistant, the project directory next to
`AGENTS.md`): a file inside the working copy changes the implementation's inputs
and makes check, conformance and baseline stale. If a key is missing, `invalid_request`
names it in its message (`… missing: cleanup_wait_seconds`); extra keys are only counted.

**Done when:** `status` shows `"control_state": "finished"` and
`result.core.suspended` is `false`, `export` ends `"state": "succeeded"`,
and `lab project status demo` shows the `evolve` scenario's first cycle
with every step `done`.

### Step 7. Your own project

**Do:** register your own agent's working copy the same way, instead of the
demo:

```bash
lab --json project create acq --name Acquiring --path /home/you/acquiring \
  --root lab --impl agent=lab/agent,package=bundle-request.json
```

The package entry is resolved from the materials root (`--root`). When `package.present` is false, its
`source.reason` and `source.entry` say why and where Lab looked.

For the `spec` step of your own agent, write two files in the materials root (`--root`): `SPEC.md` and
`spec.answers.json`. Print the schema of the answers file with `lab scenarios schema connect spec-answers/1`
and follow the `#spec` section of the `connect` skill (`evolution-lab-scenario-connect`). Run the `lab project
confirm` command of step 3 only after `lab project status` shows `spec` as `done`: a confirmation made before the
answers file exists links a missing file and goes stale at once.

Then walk steps 3–6 above against your project id and implementation. The exact fields — implementations, package entry, links,
mutations, facts and every route and error code — are in
[Projects API](../../lab/docs/api/projects.md).

**Done when:** `lab project show <id>` reports `"state": "ready"` and
`lab project status <id>` lists the same four scenarios you saw for the demo (`restructure` unavailable on a fresh project).

### Step 7a. Port an implementation onto a template

**When:** you are restructuring an agent onto a template, such as the Sylph
skeleton. Lab knows no template by name: you pass a git URL (or the absolute
path of a local repository) and, after `@`, an optional branch, tag or commit.

**Do:**

```bash
lab project port acme --from agent --template https://git.example.com/team/sylph.git@v1
lab project show acme
lab project facts acme --implementation agent-port      # implementation.ported holds
lab project open acme                                    # impl/agent-port next to work/ and materials/
```

**What you get:** a new implementation `agent-port` (`--id` names it otherwise),
registered as ported from `agent`, whose place is its own git repository at
`~/.local/share/evolution-lab/ports/acme/agent-port` (or at the absolute
`--into` directory you gave, outside your working copy and the Lab home). It
holds the template's tracked files at one commit, one commit by you (or by Lab,
if your git identity is not set) and the tag `lab/port/created`. There is no
remote: delivering the port, and moving the agent's code into it, are yours.
`lab project show` lists the template, ref and commit under the implementation's
`port`. A credential in the template URL is never stored or shown. The snapshot
is `git archive`: the template's `.gitattributes` `export-ignore`/`export-subst`
apply, and Git LFS files arrive as pointer files. The default id `<source>-port[-N]`
must fit the 40-character id grammar; for a long source id pass `--id`. To carry the agent's behaviour into the port, walk the `restructure`
scenario (`lab project status acme --implementation agent-port --scenario restructure`,
skill `evolution-lab-scenario-restructure`); `connect` the port afterwards for its
quality number. Then set the
port's package with `lab project impl set-package` and walk the `connect`
scenario against `--implementation agent-port`. The exact checks, destinations
and errors are in [Projects API](../../lab/docs/api/projects.md#ports-lab-project-port).

### Step 7b. The agent changed in the team repository

**When:** you come back to a project made with `--clone` weeks later and the team has pushed new
commits (a skill, a tool, a prompt edit). The working copy under `clones/<id>/` stays at the commit it was
cloned at until you ask; nothing fetches in the background.

**Do:**
```bash
lab --json project sync acq                       # fetch and report; the working copy does not change
lab project sync acq                              # the same, as a short readable report
lab --json project sync acq --apply <sha>         # merge exactly the commit the report named
lab --json project status acq                     # what went stale because the files changed
```

Read the report first: the `mode` (`fast_forward` when `lab/acq` has no commits of its own, `merge` when it has,
for example the connection package), the commits and files, and `dirty`. Apply merges the `upstream.sha`
you read, nothing newer. A conflict, or a local untracked or ignored file the team now tracks, is refused with
`merge_conflict` and the copy stays exactly as it was; `working_copy_dirty` means a tracked file was edited by hand
(commit or revert it first). Facts and confirmations are not edited: the changed files make them stale, and
`project status` lists what to re-check. `--rev R` checks another branch, tag or commit instead of the source's
default branch. Only a Lab clone syncs; a directory you registered with `--path` is yours to update with git.

**Done when:** `sync --apply` answers a `lab-project-sync-result/1` and `project show acq` has
`working_copy.source.base_revision` equal to the sha you applied. Details: [Sync](../../lab/docs/api/projects.md#sync-lab-project-sync).

### Step 8. Work with an assistant

**Do:** generate the project's assistant workspace. It is rebuilt whole on
every call — do not edit it by hand, edit the working copy instead.

```bash
lab --json project open demo
```

This writes `<home>/workspaces/demo/`: `AGENTS.md` (and `CLAUDE.md` =
`@AGENTS.md`), `.claude/skills/` and `.agents/skills/` with ten reference
skills and the four scenario skills (`connect`, `create-skill`, `evolve`
and `restructure`), `work` and `materials`
symlinked to the project's working copy and materials root, and
`.lab-workspace.json` recording the pair, the socket and the two catalogue
digests. When the `mcp` extra is installed where the service runs, it also
holds the Lab MCP server's client files: `.mcp.json` (Claude Code),
`.codex/config.toml`, `.qwen/settings.json` and `.gigacode/settings.json`. See
[In a Lab project workspace](mcp.md#in-a-lab-project-workspace) for approval,
Codex trust and restarting the assistant.

The workspace includes the Owner and service guides. Relative links in its skills open the corresponding workspace files.

The generated `AGENTS.md` tells the assistant to load the scenario skill and read project
status before each step. A step is done when project status says so. Keep request files
outside the working copy.

`lab project open demo --autonomy ask` is the default. A `human_only` step needs the human's
confirmation at `ask`. The assistant uses the MCP tool for an asking action, or asks in
the conversation before running the CLI command. Use `--autonomy yolo` to let the
assistant run the requested action without a Lab question. Restart or reconnect the
assistant after changing the level.

For an assistant that does not read `AGENTS.md` on its own, print it
instead of the path:

```bash
lab project open demo --print
```

**Done when:** `<home>/workspaces/demo/AGENTS.md` exists and its "Start
here" section names a working `lab project status demo` command. The command
is absolute (the service's interpreter with `-E -P -m
evolution_lab.cli`) and carries `--socket <path>`. If the extra is installed and
you have approved the server in your assistant, its `project_status` tool with
`project` = `demo` answers too.

## Reference material

### The socket lives in the Lab home

`lab serve` without `--socket` listens on `<home>/service.sock`. The home is
`--home`, else `$LAB_HOME`, else `$XDG_DATA_HOME/evolution-lab`, else
`~/.local/share/evolution-lab`. Every client command resolves the socket the
same way: `--socket` first, then the global `--home`, then `$LAB_HOME`, then
the XDG default, then the fallback — so as long as you never pass `--socket`
and always pass the same `--home` (or none) to both `lab serve` and the
client, you never have to look the socket path up.

If the home-derived path would not fit a Unix socket address (about 107
bytes), Lab refuses rather than choosing a different path silently: the CLI
with a usage error, `lab serve` with `socket_path_too_long` (stage
`transport`). Pass `--socket` yourself with a shorter path when that
happens — typically because `--home` (or `$XDG_DATA_HOME`) points somewhere
deep.

`lab guides` and `lab scenarios` never touch the service: they reject both
`--socket` and `--home`.

### The demo's deterministic suspension

The demo bakes in, at creation time, the loopback port, the service
environment digest and the service interpreter path; after the service
environment changes (rebuilt wheels, a moved venv) or when the port gets
taken, create a new demo.

While `<home>/clones/demo.fault` exists, every turn of the demo agent fails
as the target's own fault. A campaign running against it suspends instead of
failing, with `result.core.suspensions[-1].reason == "target_unstable"` in
`lab status`. Use this to rehearse `resume` cheaply, without waiting for a
real infrastructure failure:

```bash
touch <home>/clones/demo.fault
# start a campaign; watch it suspend

lab --json target stop TARGET --generation GENERATION --wait
rm <home>/clones/demo.fault
lab --json project target demo --bundle BUNDLE_DIGEST   # a fresh, healthy target
lab --json task status TASK_ID --generation GENERATION
lab --json resume OPERATION --target TARGET --generation GENERATION
```

The flag sits next to the working copy, outside every Bundle root, so
creating or removing it never changes a Bundle digest and never stales a
fact.

### What Lab does not do

Lab never writes into your team's repository and never pushes. It writes into a
working copy only when it clones one (`--clone`) and when you run `lab project sync`,
which fetches and merges into that clone (`clones/<id>/`) and never touches a directory you
registered with `--path`. `project open` and `project demo` both
create files only under the Lab home (`workspaces/`, `clones/`); confirming
a spec, accepting a stale fact or running a campaign never touches the files
you pointed `--path` (or `--clone`) at (Lab changes a clone only through clone creation and `lab project sync`; assistants and people commit to `lab/<id>`), beyond what a campaign's own export
ZIP contains — and that ZIP is never applied for you.

`lab project port` writes only under the Lab home (`ports/`, `tmp/`) or at the
`--into` directory you name outside the working copy; Lab never deletes a port
directory, not even when the implementation is archived.

**Next:** [Read the results](results.md) for what a campaign's status,
report, diff and export mean in full, and [Projects API](../../lab/docs/api/projects.md)
for the exact project, link, fact and scenario shapes.

## Reference

- [Projects API](../../lab/docs/api/projects.md) — the Lab home, the registry, links,
  facts, confirmations, acceptance, the scenario catalogue and every
  `/v1/projects` route and error code.
- [Local service and CLI reference](../../lab/docs/api/service-s5.md) — starting the
  service, global flags, mutation keys.
- [CLI and local service](cli.md) — the same actions for a single Bundle,
  without a project.

### First-measurement readiness after campaign changes

In dev66, `lab-project-facts/5` computes `baseline.measured` from the original
evaluation Bundle's measurement basis or an exact acceptance chain. Campaign-only
changes can preserve it; changed materials still require fresh check/conformance
for `connected`. Use these shared `holds` values for readiness; the latest failed
terminal evaluation still shadows older success. To select by schedule/metrics,
use `lab bundle measurements DIGEST`: its candidates include unfinished results.
See [basis and acceptance boundaries](../../lab/docs/api/projects.md#facts-lab-project-facts5).
This is within one producing pair/state; old runs remain with their wheels.
