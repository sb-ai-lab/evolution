# Projects API: Lab home, registry, facts and acceptance

> **Who this is for.** Anyone who registers an agent project with Lab and reads its
> facts, from the `lab project` command or from another program over the local
> service (auto-evolve, an assistant, your own script). For the service itself
> (starting it, the general routes, mutation keys) read the
> [local service reference](service-s5.md) first. This page is the exact reference
> for the project layer: the Lab home, `lab-project/2`, links, the materials
> revision, facts, confirmations, acceptance of changes, the `/v1/projects` routes,
> their errors and the `lab project` commands.

A **project** is the agent you work on, as Lab sees it: a working copy on disk, a
materials root inside it (corpus, scorer, launch file, specification), and one or
more **implementations** of the agent, each with its own package entry. The project
layer records which Bundle, PreparedExecution, target and operation belong to which
project and implementation, and computes **facts** from those records — "check
passed", "baseline measured", "connected" — the same way for every client.

The registry lives in the **Lab home**, above version pairs: a new pair (a new state
root) keeps every project, its history and its facts. Lab never writes into the
working copy, except an explicit [`lab project sync`](#sync-lab-project-sync) of a Lab clone
(PDR-0024).

Available from Lab `0.0.3.dev28` (projects, part 1). The scenario catalogue and the
project status (`lab-project-status/6`, below) are part 2. The assistant workspace
(`lab project open`) and the free demo project (`lab project demo`) are part 3,
below.
Python users get the same behaviour from `evolution_lab.projects` (`Projects.open`,
`ProjectSession`). For a guided walkthrough of the whole thing, start with
[Projects and scenarios](../../../docs/guide/projects.md).

Lab `0.0.3.dev35` (wave 1, restructure support) adds
[ports](#ports-lab-project-port) (`lab project port`), scenario artifact
[schemas](#scenario-schemas), the `implementation.ported` fact,
`lab-scenario/2`, `lab-project/2`, `lab-project-facts/4` and
`lab-project-status/5`. Lab `0.0.3.dev38` serves `lab-project-status/6`
([What `/6` added](#status-lab-project-status6)); the old majors, `/5` included,
are no longer served. Current dev66 facts are `lab-project-facts/5`; see
[the basis-aware fact contract](#facts-lab-project-facts5).

## The Lab home

The home is one private directory:

| Entry | Content |
| --- | --- |
| `projects.sqlite3` | The registry: projects, links, inventories, the hash cache, confirmations, acceptances. |
| `clones/<project>/` | Working copies that Lab cloned (`--clone`), and the free demo project's working copy (`clones/<id>/`, below). Lab changes a clone only through clone creation and [`lab project sync`](#sync-lab-project-sync); assistants and people may commit to `lab/<id>`. |
| `workspaces/<project>/` | The assistant's project directory, rebuilt whole by `lab project open` (below). |
| `ports/<project>/<port-id>/` | A port repository that `lab project port` created at the default destination ([Ports](#ports-lab-project-port)). Lab never deletes it. |
| `tmp/port-*` | A template fetch in progress (a URL template); removed when the fetch ends and, if Lab's leftover mark is next to it, by the next writing open ([Leftovers and cleanup](#ports-lab-project-port)). |
| `service.sock` | The service socket, when `lab serve` was started without `--socket` (W49; [Local service reference](service-s5.md#lab-home)). |
| `home.lock` | The single-writer lock. |

- **Location.** `lab serve --home DIR`. Without `--home`: `$LAB_HOME`, else
  `$XDG_DATA_HOME/evolution-lab`, else `~/.local/share/evolution-lab`. The path must
  be absolute without `..` or symlinks; the directory must belong to you with mode
  `0700`. Lab creates it (and `clones/`, `workspaces/`; `ports/` and `tmp/` when a port needs them) on first use. Otherwise:
  `home_invalid`. A home-derived socket path that would not fit a Unix socket
  address (108 bytes, including the terminating NUL) is refused with
  `socket_path_too_long` rather than silently placed elsewhere: pass `--socket`
  with a shorter path.
- **One writer per home.** `lab serve` (or a writing `Projects.open(home)` in Python)
  holds an exclusive `flock` on `home.lock` for its whole life. A second writer gets
  `home_busy`: `lab serve` prints one error line and exits `1` **before** it creates
  the state directory or the socket. To run two pairs at the same time, give them
  two homes. `Projects.open(home, read_only=True)` reads without the lock.
- **State roots stay separate.** `lab serve --state S --home H`: the state root is
  per pair and can live anywhere, including inside the home (for example
  `H/pairs/dev28`). It must not overlap `clones/` or `workspaces/`: a `--state` that
  is `H/clones` or `H/workspaces`, lies inside one of them, or contains them (the home
  itself, for example) is refused. `lab serve` prints one error line
  (`invalid_argument`) and exits `1` before it creates the state directory, the socket
  or anything in the home.
- **Tests and scripts** that start a service should pass their own `--home` (or
  `LAB_HOME`). Otherwise they take your real home and its lock — the next service
  on it gets `home_busy`. A Python `ServiceRuntime` has no default and refuses to
  start without `home_directory`; only `lab serve` falls back to the default home.

**Registry versions.** `PRAGMA user_version` is `major * 1000 + minor`.
The current version is `1004` (dev60): minor 4 adds `input_environments`, which
stores environment declarations and their sources by input digest without
changing the frozen project/link JSON records. Earlier versions are `1003`
(dev35: minor 3 adds `ports` and its template provenance, written with the port's
implementation), `1002` (dev29: minor 2 adds `link_resume_targets`, written in the
same transaction as the link's `last_seen`), and `1001` (dev28: minor 1 adds
cycle exports).

Opening an existing older registry of the same major with a **writer** backs it
up to `projects.sqlite3.bak-<oldversion>` before applying the additive migrations.
For example, the first dev60 writer on a `1003` home creates
`projects.sqlite3.bak-1003` and upgrades it to `1004`. A read-only open keeps the
existing schema and does not migrate it. Migrations only add tables and columns;
older pairs keep reading and writing the fields they know. A registry of a newer
**major** is refused with `registry_newer` and nothing is written; an older major
or an unreadable file is `registry_invalid`.

The Lab home is shared across pairs, so even when dev60 uses a fresh **Owner
state** directory, opening an existing home with a writer upgrades its registry.
Owner state has a separate contract: earlier Owner schemas are refused without
migration; old runs stay with their producing wheels and state.

## The project `lab-project/2`

The wire form is the stored record `lab-project/1` (frozen, ADR-0017: the record in
the registry does not change within its major) plus `schema: "lab-project/2"` and one
more key, `port`, on every implementation: `null`, or `{template, ref, commit,
created_at}` for an implementation made by [`lab project port`](#ports-lab-project-port)
(`template` has URL credentials replaced by `***`). `GET /v1/projects` answers
`lab-project-list/2` `{schema, projects}` with the same project form. Every route that
returns a project (create, demo, show, update, port) answers `lab-project/2`.

```json
{
  "schema": "lab-project/2",
  "id": "acq",
  "name": "Acquiring",
  "state": "ready",
  "failure": null,
  "created_at": "2026-09-27T17:55:00Z",
  "archived": null,
  "record_version": 1,
  "working_copy": {"path": "/home/you/acquiring", "created_by_lab": false, "git": true, "source": null},
  "materials_root": "lab",
  "exclude": [],
  "implementations": [
    {"id": "agent", "name": "agent", "place": "lab/agent", "origin": {"kind": "connected"},
     "package_entry": "bundle-request.json", "archived": null, "port": null},
    {"id": "agent-port", "name": "agent-port", "place": "/home/you/.local/share/evolution-lab/ports/acq/agent-port",
     "origin": {"kind": "ported_from", "implementation": "agent"}, "package_entry": null, "archived": null,
     "port": {"template": "https://git.example.com/team/sylph.git", "ref": "v1",
              "commit": "3f0c9a51b7d2e84a6c1f0b9d5e7a2c4486d1b3e0", "created_at": "2026-10-01T09:30:00Z"}}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `id` | `^[a-z][a-z0-9-]{1,39}$`, fixed for life and never reused (archived ids stay taken). Omitted at creation: Lab picks `adjective-noun-NN`, e.g. `quiet-river-42`. |
| `state`, `failure` | `creating`, `ready` or `failed` (with `failure`, e.g. `clone_failed`, `interrupted`); see the lifecycle below. |
| `record_version` | Grows by one with every mutation. |
| `working_copy` | `path` (absolute); `created_by_lab` (a Lab clone); `git` (the path itself has a `.git`); `source` — `{repo, base_revision, branch}` for a clone (`base_revision` is the last source commit in the working copy: the one cloned, then the one `lab project sync` merged), `null` for a directory you registered. `repo` is stored and returned with URL credentials replaced by `***` (`https://***@host/…`); the real URL is used only for the clone itself. |
| `materials_root` | A path inside the working copy. The evaluation (corpus, scorer, world), the launch file and the specification live here and are shared by all implementations. |
| `exclude` | Globs relative to the working copy, applied **only** to the materials revision. |
| `implementations[]` | Same id grammar, unique within the project including archived ones. `place` is a path inside the working copy or an absolute path (an implementation in its own checkout). `origin` is `{"kind": "connected"}`, `{"kind": "created"}` or `{"kind": "ported_from", "implementation": "<id>"}`. `package_entry` is a path inside the materials root, or `null` (no package yet). `port` (wire form only) is `null` or the template provenance of a port. |
| `archived` | `null` or `{"at", "reason"}` — on the project and on each implementation. |

**The package entry** is the body of `lab-service-bundle-request/2` **without**
`schema`. Relative paths in it resolve from the entry file's directory, so the
package moves with the working copy:

```json
{
  "source": {"kind": "campaign", "manifest_path": "manifest.json", "launch_path": "launch.json"},
  "environments": [{"kind": "service"}],
  "env": {}, "secret_names": [],
  "roots": [{"alias": "project", "source_path": "."}],
  "path_bindings": []
}
```

At least one root must equal, contain or lie inside the implementation's `place`;
otherwise the Bundle is refused with `package_mismatch`. This keeps the facts of
implementation B from coming from a package built around the code of A.

### Creating a project

- `--path DIR` (source `{"kind": "path", "path": DIR}`): an existing absolute
  directory; the project is `ready` at once. `git` is `true` only if `DIR/.git`
  exists; without git there are no git facts (they are absent, not false).
  Otherwise `working_copy_unavailable`.
- `--clone REPO [--rev R]` (source `{"kind": "clone", "repo", "rev"}`): the record is
  written as `creating` (the id is taken), `git clone` runs into
  `clones/.staging-<id>-<nonce>/` with `GIT_TERMINAL_PROMPT=0`, a branch `lab/<id>`
  is made from `R` (default: the source's default branch), and the directory is
  renamed to `clones/<id>/`; then `ready`. A failure removes the staging directory
  and leaves the project `failed` with `clone_failed`. Lab never pushes; it fetches only on
  [`lab project sync`](#sync-lab-project-sync) (PDR-0024).
  - **ssh.** Your own ssh command is used as is: `GIT_SSH_COMMAND` or `GIT_SSH` in the
    service's environment, or `core.sshCommand` in the git config the clone reads
    (system or global, as `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` may place them). Only
    when none is set does Lab run ssh as `ssh -o BatchMode=yes`, so a password or
    passphrase prompt fails instead of hanging.
  - **Repository selectors.** Every git command of a clone or a port runs without the
    service's variables that choose or locate a repository (`GIT_DIR`, `GIT_WORK_TREE`,
    `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
    `GIT_COMMON_DIR`, `GIT_NAMESPACE`, `GIT_CEILING_DIRECTORIES`,
    `GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_PREFIX`, `GIT_CONFIG_PARAMETERS`,
    `GIT_CONFIG_COUNT`, and the rest of `git rev-parse --local-env-vars`), so git works
    only in the directory Lab names and never in another repository such as the
    working copy.
  - **Which `R`.** The first that names a commit in the fresh clone wins: the remote
    branch `refs/remotes/origin/R`, the tag `refs/tags/R`, then `R` itself as a commit
    id or other revision. A branch whose name looks like a commit prefix (`cafe`) is
    the branch. An `R` that starts with `-` is `invalid_request`, before git runs and
    before the project record is written.
  - `lab/<id>` is created with `--no-track`: it has no upstream.
  - **Diagnostics.** The `clone_failed` body stays closed. The last 20 lines of git's
    stderr go only to the service log, with URL credentials replaced by `***`
    (`https://***@host/…`). `lab serve` keeps its stderr JSON lines: the tail is one
    diagnostic line `{"error": {"code": "clone_failed", "stage": "project", "message":
    "git clone exited 128; stderr tail:\n…"}}`, printed with the other diagnostics when
    the service stops. Outside the service (the Python API) it is a `WARNING` of the
    logger `evolution_lab.projects`.
- After a crash, the next service start turns a `creating` project into `failed`
  (`interrupted`) and removes its staging directory — or into `ready` if
  `clones/<id>/` is already in place (the rename is the last step).
- A `failed` project was never `ready` and has no links: `create` with the same id
  replaces it and tries again. Other existing ids are `id_taken`.
- `creating` and `failed` projects are listed with their `state`; routes that work
  with a project (Bundle, facts, …) answer `project_not_ready`.

### Mutations

`PATCH /v1/projects/{id}` carries one mutation in
`lab-service-project-update/1`:

```json
{"schema": "lab-service-project-update/1",
 "mutation": {"op": "move_implementation", "implementation": "agent", "place": "agent-v2"},
 "expected_record_version": 3}
```

| `op` | Keys | Refused while an operation is active |
| --- | --- | --- |
| `rename` | `name` | — |
| `set_working_copy` | `path` (absolute, existing) | any operation of the project |
| `set_root` | `materials_root` | any operation of the project |
| `set_exclude` | `exclude` (list of globs) | — |
| `add_implementation` | `implementation` (`{id?, name?, place, origin?, package_entry?}`) | — |
| `rename_implementation` | `implementation`, `name` | — |
| `move_implementation` | `implementation`, `place` | an operation of that implementation |
| `set_package` | `implementation`, `package_entry` (or `null`) | — |
| `archive_implementation` | `implementation`, `reason` (string or `null`) | an operation of that implementation |
| `archive` | `reason` (string or `null`) | any operation of the project |

- **Active** means an operation of the current pair in `reserved`, `running` or
  `stopping`; the refusal is `active_operation`. A suspended campaign in
  `finished` is not active. The check reads Owner's own `client_ref` labels, not
  the registry, so a live operation blocks mutations even before its link exists.
- `expected_record_version` (or `null`): a different current value is `conflict`.
- Moving a place, the root or the working copy does not rewrite history: facts
  obtained at the old place become stale with `place_changed`.
- Archived projects and implementations leave the default lists; they stay readable
  by id and with `?archived=1`, with all links, operations and results. Work routes
  on an archived project or implementation answer `archived`.

Examples below that name dev28/a17 or dev35/a21 retain those historical
producing-pair values and nested result snapshots. They do not prescribe a
current install or rewrite the old binding's sources. Current operation results
use `lab-operation-result/2`; current-pair routes and baseline rules are stated
separately below.

## Links `lab-project-link/1`

A link says "this Bundle / PreparedExecution / target / operation was made for this
project and implementation, on these inputs, by this pair". Only the project routes
(and the Python `ProjectSession`) write links; the general routes and their shapes
do not change.

```json
{
  "schema": "lab-project-link/1",
  "kind": "operation",
  "project": "acq",
  "implementation": "agent",
  "pair": {"lab": "0.0.3.dev28", "core": "0.3.0a17"},
  "state_root": "/home/you/lab-run/state",
  "created_at": "2026-09-27T17:55:08Z",
  "binding": {
    "working_copy": "/home/you/acquiring",
    "materials_root": "lab",
    "place": "lab/agent",
    "materials_revision": "sha256:df8e7437…",
    "inputs": {"digest": "sha256:bafc77c5…",
               "sources": [{"alias": "project", "path": "lab"}, {"path": "lab/manifest.json"}, {"path": "lab/launch.json"}]}
  },
  "bundle_digest": "155ea76e…",
  "prepared_digest": "109890eb…",
  "object_id": "c67008132a6146d9ad585f880677f0ba",
  "operation_kind": "evaluation",
  "last_seen": {"at": "2026-09-27T17:55:12Z", "control_state": "finished", "kind": "evaluation",
                "results_state": "present", "result": {"schema": "lab-operation-result/1", "…": "…"}, "resume": null}
}
```

- `kind`: `bundle`, `prepared`, `target` or `operation`. `object_id` is the Bundle
  digest, the PreparedExecution digest, the target id or the operation id;
  `operation_kind` is set only for `operation`.
- `binding` is a snapshot taken when the Bundle was made. `inputs` is the inventory
  of the **source** files the Bundle consumed — its roots, the manifest or
  evaluation input, the launch file — under the file-set rule below, taken on the
  source side (Bundle relocation rewrites documents, so the Bundle's own inventory
  cannot be compared). Paths are relative to the working copy, or `@/abs/path`
  outside it. `prepared`, `target` and `operation` links inherit the binding of
  their Bundle link.
- For new dev60 bindings, the consumed inputs also include the ordered environment
  declarations (`executable`, `wheel_paths`, `runtime_files`, or the service marker)
  and the bytes of declared wheels/runtime files, including external `@/abs/path`
  sources. The interpreter path is declared; interpreter bytes are not inventoried.
  Declaration changes and changed wheel/runtime bytes invalidate these facts.
  Bindings recorded before dev60 keep their original sources and digest meaning;
  they are not silently reinterpreted or expanded.
- `last_seen` of an operation: `{at, control_state, kind, results_state, result,
  resume}`, where `resume` is `{state, reason}` only — the target requirement is
  not part of `last_seen` (it has its own table since registry minor 2). For a finished fixed evaluation `result` is frozen at the first terminal
  observation (evaluations are never continued), so a later `delete-results` or an
  unreadable journal does not undo the measurement. `last_seen` of a target:
  `{at, tasks: [{task_id, action, state, target_id, bundle_digest, client_ref,
  position}]}` — the `check` and `conformance` tasks on that target, `position`
  being Owner's recording order. In either snapshot, `at` is when Lab wrote that
  snapshot, meaning when its content last changed; repeated reads leave it intact.
- Making the same Bundle or PreparedExecution again (same digest) refreshes the
  stored link's `binding`; derived links take the newest source link of their pair
  and state root.

**Who is the source of truth.** The pair's state is the truth for its own pair; the
links are a durable projection of it.

- `bundle`, `prepared`: the action runs first, then the link is written. If writing
  the link fails, the route answers `link_failed` (a closed body without the
  digest). Both actions are content-addressed, so repeating the request writes the
  link. **Repeat with a new request id**: a keyed replay of a failed project
  mutation returns the stored failure (ADR-0009 forgets only `not_found`). The CLI
  makes a fresh key on every invocation, so running the command again is enough.
- `target`, `operation`: the project route passes Owner an opaque label
  `client_ref = {"project", "implementation"}`, written in the same transaction as
  the target or operation row. If the link cannot be written after Owner accepted,
  the response carries `link: null` and **synchronisation** writes it later.
- **Synchronisation** runs when the service starts, before it stops (so the final
  outcome of operations is kept before a repin), and on every read of a project's
  facts or operations. It adds links for labelled operations and targets of the
  current pair and updates `last_seen` when its content changes, including the check
  and conformance outcomes of project targets.
- **Shutdown can take up to about twice `--shutdown-wait-seconds`.** Before the last
  synchronisation the service waits, up to that budget, for the operation it asked
  to stop to settle (so its link keeps the final state, not `stopping`); closing the
  Owner then waits up to the same budget again.
- **After a repin or a new state root** the history shows the last synchronised
  snapshot. Lab never opens a previous state root: `lab status <old operation>` on the
  new service is `not_found`, while `lab project operations` still lists it with its
  `last_seen`.
- `GET /v1/operations?project=P[&implementation=I]` lists `lab-operation-list/8` of the
  **current pair only**, by Owner's `client_ref`. An unknown project gives an empty
  list, not an error; `implementation` without `project` is `invalid_request`. For
  the history across pairs use `GET /v1/projects/P/operations`.

## Materials revision `lab-materials-revision/1`

```json
{
  "schema": "lab-materials-revision/1",
  "digest": "sha256:df8e7437…",
  "places": [
    {"role": "materials_root", "path": "lab", "digest": "sha256:bafc77c5…"},
    {"role": "implementation", "id": "agent", "path": "lab/agent", "digest": "sha256:1eb93f61…"}
  ],
  "files": 11,
  "git": null
}
```

- **Places:** the materials root and the place of every active implementation. The
  union of their inventories is taken once (a nested place is not counted twice);
  each place's `digest` covers its own subtree. `files` counts regular files and
  shows the scale.
- **A nested materials root (dev38).** The `digest` of an `implementation` place leaves
  out the subtree of the `materials_root` when that root lies strictly inside the
  place: with `materials_root: "lab"` and `place: "."`, writing `lab/SPEC.md` changes
  the digest of the `materials_root` place and the revision `digest`, not that of the
  implementation, so a confirmation linked to `implementation:<id>` stays valid. Equal
  paths, and a place inside the materials (`lab/agent` under `lab`), are unchanged;
  `exclude` applies first. Paths are compared lexically (the walk follows no
  symlinks). The union of inventories, the revision `digest`, `files` and a Bundle are
  unchanged — a Bundle rooted at the place still contains the nested materials. A
  project with nested materials sees its implementation place digest change once on
  moving to dev38.
- **Inventory entry**, the same record a Bundle uses: path (relative to the working
  copy), type, executable bits, size, sha256. A symlink is recorded with its
  `target` and not followed (a Bundle still refuses it with `unsupported_input`).
- **One file-set rule for the revision and for Bundles:** directories `.git`,
  `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache` and files `*.pyc`,
  `*.pyo` never enter; `.gitignore` is **not** honoured (corpora and data are often
  ignored by git, and their changes must change the revision). The project's
  `exclude` applies to the revision only; operation inputs are always compared
  without it.
- `git`: `null` without git; otherwise `{commit, clean}` — HEAD, and whether
  tracked and untracked non-ignored files are unchanged.
- **Cost.** sha256 is recomputed only for files whose `(size, mtime_ns, inode,
  ctime_ns)` changed (a hash cache in the registry); the walk with `stat` runs on
  every request. Remove heavy directories such as `node_modules` with `exclude`.

## Staleness

`stale_reason` is `null` or `{codes, message, changes}` with **every** applicable
code. The list of codes below is a **closed contract for clients** (dev30): a code is added or
changed only with a new schema version of the payloads that carry it, so a client may switch on the
codes exhaustively and treat anything else as a protocol mismatch:

| Code | Applies to | When |
| --- | --- | --- |
| `other_pair` | check, conformance, baseline | The evidence was obtained by another version pair. |
| `place_changed` | check, conformance, baseline | The working copy, materials root or implementation place differs from the link's binding. |
| `inputs_changed` | check, conformance, baseline | The current inventory of the link's `inputs.sources` differs from the recorded one (rule R2: only files the operation consumed count, so editing a README outside the package does not demand a new baseline). |
| `digest_changed` | confirmations | The attested file, a linked file, folder tree, place or the revision has other bytes. |
| `catalogue_changed` | confirmations | A linked catalogue (`requirements` or `scenarios`) changed. |

`changes` breaks the difference down: `renamed` (same content, type and bits under a
new path), `modified`, `added`, `removed` — each list capped at 20 — and `counts`
with the full numbers. `message` is a human-readable summary (currently in Russian);
programs should read `codes`.

```json
{"codes": ["inputs_changed"], "message": "изменились входы (1 переименовано)",
 "changes": {"renamed": [["lab/agent/prompt.md", "lab/agent/prompt-v2.md"]], "modified": [], "added": [],
             "removed": [], "counts": {"renamed": 1, "modified": 0, "added": 0, "removed": 0}}}
```

## Facts `lab-project-facts/5`

`GET /v1/projects/{id}/facts[?implementation=I]` (CLI `lab project facts`). Facts are
not stored; they are computed on each request (after a synchronisation), and every
computed fact is always present.

```json
{
  "schema": "lab-project-facts/5",
  "project": "acq",
  "implementation": "agent",
  "pair": {"lab": "0.0.3.dev28", "core": "0.3.0a17"},
  "revision": {"schema": "lab-materials-revision/1", "…": "…"},
  "facts": [
    {"id": "check.passed", "subject": {"implementation": "agent"}, "holds": true, "value": true,
     "source": {"kind": "task", "id": "b812e1f6…", "pair": {"lab": "0.0.3.dev28", "core": "0.3.0a17"},
                "inputs_digest": "sha256:bafc77c5…"},
     "stale_reason": null, "carried_over": []}
  ]
}
```

- `subject` — what the fact applies to: `{"implementation": id}` or `null` (the
  project). Which bytes stand behind it is in `source`.
- `holds` is `true` when `value` is truthy (`true`, a non-empty object or list) and
  `stale_reason` is `null`. Clients should read `holds`, not derive it.
- `carried_over` — `[{acceptance, reason, across_pairs}]`: the fact holds because a
  person accepted the change (below), not because it was measured again.

Without `--json`, `lab project facts` prints a summary line and then one line per fact:
`fact <id> holds=yes|no`, then `stale=<codes>` when the fact is stale (codes joined by
`,`) and `carried` when `carried_over` is not empty. Non-printable characters in
values print as `?`.

```text
lab-project-facts/5
project=acq implementation=agent facts=11
fact implementation.available holds=yes
fact implementation.ported holds=no
fact materials.revision holds=yes
fact package.present holds=yes
fact package.valid holds=yes
fact check.passed holds=no stale=inputs_changed
fact conformance.passed holds=yes carried
fact baseline.measured holds=no stale=other_pair,inputs_changed
fact connected holds=no
fact campaigns holds=no
fact spec.confirmed holds=yes carried
```

**Computed facts** (reserved names):

| Fact | Subject | Value and source |
| --- | --- | --- |
| `implementation.available` | implementation | The implementation is active and its place is a readable directory. |
| `implementation.ported` | implementation | The implementation's `origin.kind` is `ported_from` **and** the source implementation is still registered (an archived source counts). Source `{"kind": "origin", "implementation", "reason"}`; `reason` is `null` when it holds, `not_ported` (any other origin; `implementation` is `null`) or `source_missing` (the named source is not in the project). It never goes stale. The name is reserved for confirmations (`reserved_claim`). |
| `materials.revision` | project | `{digest, git}` of the current revision. |
| `package.present` | implementation | The package entry file exists. `source`: `{"kind": "package_entry", "entry", "reason"}` — `entry` is the implementation's `package_entry` relative to the materials root (or `null`), `reason` is `null` when the file exists, else `not_configured`, `missing` or `not_a_file` (dev46). |
| `package.valid` | implementation | **A form check without environment capture:** the entry parses with relative paths resolved; roots and path bindings are consistent (no overlap, documents under a root, known bindings); the documents parse (no duplicate keys, no NaN); a manifest is the current schema, loads with `load_manifest_v2`, has a workflow and is not a linked run; an evaluation input passes `load_evaluation_input`; the launch file parses; no document points into an excluded path; a root covers the place. The value says so: `{"scope": "form"}`. The launch-to-environment binding and the evaluation's conformance manifest are checked only when the Bundle is created. When false, `source.errors` lists `{code, detail}`. |
| `check.passed` | implementation | The latest (by Owner's recording order) `check` task on targets labelled with this implementation ended `succeeded`. |
| `conformance.passed` | implementation | The same for `conformance`. |
| `baseline.measured` | implementation | The predicate below. |
| `connected` | implementation | `check.passed`, `conformance.passed` and `baseline.measured` hold. Check and conformance use the same effective `inputs.digest`; the baseline must have that digest or the same original measurement basis as the checked Bundle. Exact acceptance still applies as below. |
| `campaigns` | implementation | Links of the implementation's campaigns across all pairs: `{operation_id, pair, control_state, resume, results_state, detail}` (`detail: "unavailable"` for another pair). `resume` is `{state, reason, target}` or `null`: `target` is the target requirement of the operation's resume verdict (`{requirement, reason, excluded}`, see [Resume](service-s5.md#resume-readiness)) or `null`. Never stale. |

**Current basis rule (dev66, facts `/5`).** A successful baseline remains current when its effective `inputs_digest` already matches the current
inputs, without requiring a readable original Bundle, a current Bundle or a source receipt. An exact,
unbroken non-revoked acceptance chain is also sufficient. Only when neither route holds can the original
evaluation Bundle's non-null measurement basis rescue the stale fact. That basis-reuse route requires
valid current materials and matching request metadata, descriptor and source receipt in this pair/state;
reading facts never creates a Bundle or captures an environment.

Compatibility limitation: request `env`, `secret_names`, paths or `path_bindings` outside `inputs_digest`
do not by themselves invalidate an unchanged-input fact. Descriptor/receipt proof protects basis reuse
after material changes, not that legacy unchanged-input route. A missing proof disables only basis reuse.
`connected` still requires all three evidence facts to hold, check and conformance to share an effective
inputs digest, and baseline to match that digest or the original basis of the checked Bundle.

The baseline source adds `bundle_digest`, nullable `measurement_basis` and
`same_bundle` (false when the current Bundle differs, or no current Bundle is
available). A null basis is unknown, not evidence of equality. Basis reuse also
requires matching pair/place and the current valid package; revocation breaks the acceptance chain.

Acceptance does not assign a new basis to its destination. For example, evaluation
on scorer A → acceptance of scorer B → campaign-only edit C still compares the
original basis A with C, so the B acceptance does not automatically cover C;
a new exact acceptance or measurement is needed. `check.passed` and
`conformance.passed` still need fresh evidence for changed materials. `connected`
uses their effective input digest plus the baseline's digest or its original
basis against the checked Bundle. Clients should read these shared `holds`
values for the first-measurement gate; discovery of
[measurement candidates](service-s5.md#bundle-measurements) does not replace it.
The latest terminal failed evaluation still shadows an older success; do not
skip it to recover a convenient old baseline. This remains bounded to the
current pair/state and does not authorize cross-pair reuse or migration.

**`baseline.measured`.** Take the latest terminal evaluation operation of the
implementation (by link order; anything outside `reserved`, `running` and `stopping`
is terminal, so `orphaned` and `cleanup_required` count). It holds only if, in this
order:

1. `kind` is `evaluation` and `control_state` is `finished` or `released`
   (otherwise `not_finished`);
2. `result.coverage` is present, `expected > 0` and
   `scored / expected >= result.min_coverage` (otherwise `coverage`);
3. `result.stopped_reason` is `null` (otherwise the existing failure code
   `stop_reason`);
4. `result.persistence.state` is `"saved"` (otherwise `persistence`).

New prepared evaluations record `min_coverage` (default `0.95`); pre-dev60
descriptors retain `1.0` and their original digests. Stopped or below-threshold
evaluations never qualify. An accepted partial baseline must show “N of M” and
its `unmeasured` list; it is not a claim about the missing scores. Current
results use `stopped_reason`; the frozen legacy snapshot reader still accepts
`stop_reason` and uses threshold `1.0` when absent.

On failure `value` is `false` and `source.failed` names the first failed condition.
The value comes from the `last_seen` snapshot frozen at completion, so a later
`delete-results` or `results_state: unavailable` does not undo it.

Only `check.passed`, `conformance.passed` and `baseline.measured` (and through them
`connected`) go stale. Facts about the present — `implementation.available`,
`implementation.ported`, `materials.revision`, `package.*` — are always computed from the live bytes.
Staleness follows the pair and the consumed inputs, not the state root: after a
restart on a new state root with the same pair, facts of the previous state root
still hold.

**Confirmed facts.** For each (claim, applicability) the latest non-revoked
confirmation gives one fact: `id` = the claim, `subject` = its implementation (or
`null` for the project), `value: true`, `source: {kind: "confirmation", id, attests:
{path, digest}, channel, author, created_at}`, `stale_reason` from its digests. `author` and
`channel` are what the client sent; Lab does not verify them. A project-level
confirmation appears in the facts of every implementation. No confirmation — no fact.

## Confirmations `lab-project-confirmation/1`

A confirmation is a client's statement about bytes in the materials root — for
example "the specification is confirmed" over the digest of `SPEC.md`. Lab records
who said it (as declared) and over which bytes; it does not check the claim.

```json
{"schema": "lab-service-confirm-request/1", "implementation": null,
 "claim": "spec.confirmed", "attests": {"path": "SPEC.md", "digest": "sha256:…"},
 "linked": [{"kind": "catalogue", "ref": "requirements"}, {"kind": "place", "ref": "implementation:agent"}],
 "channel": "auto-evolve-ui", "author": "vitaly", "note": null}
```

The record echoes the request with the current digests, plus `id` (`c-…`),
`project`, `created_at` and `revoked: null`.

- `claim` — `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`; a computed fact's name is
  `reserved_claim`.
- `implementation` — the implementation the claim applies to, or `null` for the
  whole project. `null` is never replaced by a default implementation.
- `attests` — `path` relative to the materials root (no `..`, not absolute) and,
  optionally, the `digest` the person saw.
- `linked[].kind`: `file` (a path relative to the materials root), `place`
  (`materials_root` or `implementation:<id>`), `revision` (the whole revision), or
  `catalogue` (`requirements` or `scenarios`), or `tree` (`implementation:<id>:<relpath>`,
  a folder or a single regular file relative to that implementation place). The CLI spells the last one
  `--link tree:implementation:<id>:<relpath>`.
- **Race.** Digests sent in the request (`attests.digest`, `linked[].digest`) must
  equal the current ones, or the request is `digest_mismatch`. Without them Lab
  takes the current digests. The `digest` of a `sources[]` artifact in the
  [status](#status-lab-project-status6) may be sent as `attests.digest`; a file
  replaced since then is refused `digest_mismatch`.
- The fact holds while every digest equals the current one (or the latest accepted
  state, below). For one claim and applicability the latest non-revoked
  confirmation counts; history is kept.
- Revoke: `POST …/confirmations/{cid}/revoke` with
  `{"schema": "lab-service-revoke-request/1", "reason": "…"}`; the record stays with
  `revoked: {at, reason}`. Revoking it again is a no-op: the record comes back
  unchanged, with the first `revoked`. `GET …/confirmations` lists non-revoked ones
  (`lab-project-confirmation-list/1`); `?revoked=1` lists all. An unknown record id
  currently answers `project_not_found`.

### Folder links and reading a tree

A `tree` link hashes the selected implementation folder's inventory, or a single
regular file's own entry: a skill may be one file (PDR-0032, amended 2026-11-13).
A single file's tree is one entry with `path` `.`, which no folder entry can have:
replacing the file by a folder always changes the digest.
A symlink is never a tree. Its digest is
`sha256:` followed by the SHA-256 of canonical JSON: the list of
`(path, type, executable, sha256, target)` entries sorted by relative path, UTF-8,
with compact separators and no ASCII escaping. Editing a file inside the folder,
adding or removing an entry, changing executable bits or a symlink target stales
its confirmation with `digest_changed`; an edit outside the folder does not.
The link uses the common ADR-0017 file set: `.git`, `__pycache__`, `.pytest_cache`,
`.mypy_cache`, `.ruff_cache`, `.venv`, `*.pyc`, and `*.pyo` are excluded from the
inventory and Bundle and do not affect the digest. The digest is shared with the
read described below. A missing path, an empty folder or a symlink is `tree_not_found` (404) on
confirmation and has no current digest afterward. A project exclusion that can
match the folder or a descendant (for a single file: the file or an ancestor), or any overlap with a nested materials-root cut,
refuses the link as `tree_excluded` (422), with the pattern or cut in `detail`.
In current fact state a subsequently excluded tree has no digest.

`lab --json project tree PROJECT --implementation ID --path RELPATH [--text]`,
`GET /v1/projects/{id}/tree?implementation=ID&path=RELPATH[&text=1]`, and
`await ProjectSession.tree(project_id, implementation_id, path, text=False)`
return `lab-project-tree/1`. The route is read-only and takes no mutation key.
A `relpath` cannot be empty, absolute, contain `..`, or escape the place via a
symlink. `.` reads the entire place with project exclusions and the nested
materials-root cut applied, even when a `tree` link to `.` would be refused;
for other paths the read and the link have the same exclusion refusal.

The response has `project`, `implementation`, `path`, `exists`, `digest`,
`bundle_covers`, `bundle_reason`, and `files`. Each file entry has `path` relative
to the selected folder (`.` for a single file), `type` (`file`, `directory`, `symlink`, or `other`),
`size`, `sha256`, `executable`, `target` (the unresolved symlink text or null),
and `text`. Directories have null size and hash. A missing path, an empty folder or a symlink has
`exists: false`, `digest: null` and `files: []`.

`bundle_covers` is true if the selected path is inside one of the roots from the
same Bundle request Lab would build for this implementation; false if it is not.
If that request cannot be built it is null and `bundle_reason` is the refusal code.
Without a package entry Lab may build a probe Bundle request from a valid
`lab-target-entry.json`; its actual roots determine coverage too. This read creates
no Bundle. `text` is null unless requested; then it is true only for a regular
file that decodes as UTF-8 and contains no NUL byte, false otherwise, and null for
non-files. Symlinks are not followed for classification. Lab returns no contents;
the inventory hash cache avoids reopening unchanged place files, although Bundle
request metadata must be read to compute coverage.

## Accepting changes `lab-project-acceptance/1`

A rename or a small edit of a consumed file stales a paid baseline. Acceptance is an
explicit record that carries such facts over to the new bytes: one command, with
the changes broken down, but bound to the exact new state — the next edit stales the
fact again.

```bash
lab --socket /home/you/lab-run/lab.sock project accept acq --reason "renamed the prompt file"
# prints the preview (dry_run: true), then asks: apply? [y/N]
lab --socket /home/you/lab-run/lab.sock --json project accept acq --reason "renamed the prompt file" --yes
lab --socket /home/you/lab-run/lab.sock --json project accept acq --reason "repinned to dev28" --across-pairs --yes
```

Request `lab-service-accept-request/2`: `{schema, implementation, only, across_pairs,
reason, channel, author, dry_run, expected}`. `expected` is required: `null` keeps the
previous behavior, or a non-empty array of objects with exactly `id`, `from`, `to`
from the preview's `facts`. Each request version has closed keys; `/1` remains
supported with its original keys (no `expected`). Python
`ProjectSession.accept(..., expected=None)` has the same optional comparison.
For a preview, `dry_run=True` ignores `expected`. The response record stays
`lab-project-acceptance/1`:

```json
{
  "schema": "lab-project-acceptance/1",
  "id": "a-f1bc72b28b3a1eb6",
  "project": "acq",
  "implementation": "agent",
  "facts": [
    {"id": "check.passed", "from": {"kind": "task", "id": "b812e1f6…"},
     "to": {"inputs_digest": "sha256:a7ac36ff…", "place": ["/home/you/acquiring", "lab", "lab/agent"],
            "pair": {"lab": "0.0.3.dev28", "core": "0.3.0a17"}},
     "codes": ["inputs_changed"],
     "changes": {"renamed": [["lab/agent/prompt.md", "lab/agent/prompt-v2.md"]], "modified": [], "added": [],
                 "removed": [], "counts": {"renamed": 1, "modified": 0, "added": 0, "removed": 0}}}
  ],
  "across_pairs": false,
  "reason": "renamed the prompt file",
  "channel": "cli",
  "author": "cli",
  "created_at": "2026-09-27T17:55:10Z",
  "revoked": null,
  "dry_run": false
}
```

- **What can be accepted:** a stale fact whose value was true, that is
  `check.passed`, `conformance.passed`, `baseline.measured` or a confirmed fact, and
  whose codes are all among `inputs_changed`, `digest_changed`, `catalogue_changed`,
  `place_changed`, `other_pair`. `other_pair` needs `across_pairs: true`
  (`--across-pairs`); the preview then shows both the pair change and the byte
  changes. A fact that never held, a fact about the present and a derived fact
  (`connected`, `campaigns`) cannot be accepted.
- By default the request takes every acceptable fact of the implementation; `only`
  (`--only FACT`, repeatable) narrows it. Nothing acceptable, or an `only` naming a
  fact that is not acceptable, is `not_acceptable`.
- `to` is the exact current state: for evidence facts `{inputs_digest, place, pair}`,
  for confirmations `{attests, linked}`. Each carried fact keeps its own `codes` and
  `changes`.
- **Preview:** `dry_run: true` returns the same shape with `id: null`,
  `dry_run: true`, `created_at` = the preview time and writes nothing. The CLI
  without `--yes` sends the preview, asks, and on `y` sends the real request under a
  new mutation key with `expected` equal to that preview's `facts[].{id, from, to}`;
  an explicit `--request-id` therefore needs `--yes`. With `--yes` the CLI sends
  `/2` with `expected: null`, without a preview.
- **Matching the preview:** with non-null `expected`, the exact set of `id`, `from`,
  `to` must equal the recomputed acceptance plan. Order and exact duplicates do not
  matter. A changed, extra, missing or no-longer-acceptable fact refuses the write
  with `digest_mismatch`; nothing is recorded. `inputs_changed` still means that
  consumed inputs moved between the facts read and inventory collection. Both codes
  mean repeat `dry_run`; Lab does not retry automatically. The CLI prints the error
  and a hint to run `lab project accept` again on `digest_mismatch`.
- **Guarantee and limitations:** with non-null `expected`, facts (including Owner/Core
  reads), inventories, planning, comparison and write run under the same existing
  session-wide mutation lock. The recorded `to` and its stored inventories describe
  the state shown in the preview. Owner/Core reads and input hashing hold this lock,
  so acceptance can delay mutations of other projects in the session. This lock
  cannot stop working-copy edits made outside Lab after the check, as with `confirm`.
  `/1` and `/2` with `expected: null` retain their previous order: facts and inventories
  are read before the write lock.
- **Preview in the terminal.** Without `--json` the CLI prints a summary line, then one
  line per fact the acceptance would carry, then asks `apply? [y/N]` on stderr:

  ```text
  lab-project-acceptance/1 None
  fact check.passed from=task:b812e1f6… codes=inputs_changed renamed=1 modified=0 added=0 removed=0 paths=lab/agent/prompt.md->lab/agent/prompt-v2.md
  fact spec.confirmed from=confirmation:c-1d0e… codes=digest_changed changes=none
  ```

  `None` is the preview's `id`. Each line gives the fact, its source (`from=kind:id`),
  its codes and the change counts; `paths=` shows at most the first three renamed
  (`old->new`) and then modified paths, and a fact without byte changes (a
  confirmation) prints `changes=none`. The applied record and a revoked one print
  the same lines.
- **Carrying.** A fact whose source is stale holds (`stale_reason: null`,
  `carried_over` lists the links) while a chain of non-revoked acceptances leads from
  its source to the **current** state. The next change stales it again, and its
  `changes` are relative to the last accepted state.
- Revoke: `POST …/acceptances/{aid}/revoke` with `lab-service-revoke-request/1`; the
  record stays with `revoked: {at, reason}`.
  - A revoked acceptance cuts its chain there: it and every later acceptance of the
    same fact and source stop carrying. The fact is stale again against the last
    link that remains, or against its source if none does.
  - An acceptance made after the revoke starts a fresh chain from what was left, so
    accepting again works. Times have one-second resolution: an acceptance in the
    same second as the revoke counts as made before it, and the fact stays stale —
    accept once more.
  - Revoking an already revoked acceptance is a no-op: the record comes back
    unchanged, with the first `revoked`, and a later re-acceptance keeps carrying.
- Acceptance does not change Bundles, PreparedExecutions, operations or Start
  readiness — only facts.

## Status `lab-project-status/6`

`GET /v1/projects/{id}/status[?implementation=I&scenario=S]` (CLI `lab project status
ID [--implementation I] [--scenario S]`) turns the facts of part 1 and the project's
link history into a checklist: for each scenario of the catalogue (or just `scenario`,
an unknown id being `invalid_request`), which of its steps are done, and what to do
next. One pure function
(`evolution_lab.projects.status.status_payload`) backs both the CLI and the route —
the installed end-to-end test asserts that the parsed JSON of the CLI answer equals the
parsed JSON of the route.

```json
{
  "schema": "lab-project-status/6",
  "project": "acq",
  "implementation": "agent",
  "pair": {"lab": "0.0.3.dev35", "core": "0.3.0a21"},
  "catalogue": {"version": 7, "digest": "62c77903a9cf5a1896c0efa86b29c9f7f5937263c48c86649182c1616d101969"},
  "facts": [{"…": "…"}],
  "scenarios": [
    {
      "id": "connect", "version": 2, "repeatable": false, "state": "produced", "missing": [],
      "steps": [
        {"id": "spec", "title": "Write the spec", "state": "done", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"artifact": "SPEC.md", "digest": "sha256:9f2c4e1a…"}, {"artifact": "spec.answers.json", "digest": "sha256:41b7d0c3…"}], "done_at": null},
        {"id": "spec-confirm", "title": "Confirm the spec", "state": "ready", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [], "done_at": null},
        {"id": "package", "title": "Assemble the Lab package", "state": "done", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"fact": "package.valid", "confirmations": []}], "done_at": null},
        {"id": "check", "title": "Check the connection", "state": "done", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"fact": "check.passed", "confirmations": []}], "done_at": null},
        {"id": "conformance", "title": "Run conformance", "state": "done", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"fact": "conformance.passed", "confirmations": []}], "done_at": null},
        {"id": "baseline", "title": "Measure a short baseline", "state": "done", "optional": false, "human_only": true, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"fact": "baseline.measured", "confirmations": []}], "done_at": null}
      ]
    },
    {
      "id": "evolve", "version": 2, "repeatable": true, "state": "available", "missing": [],
      "cycles": [
        {"operation_id": "c67008132a6146d9ad585f880677f0ba", "order": 1, "pair": {"lab": "0.0.3.dev35", "core": "0.3.0a21"},
         "current": true, "detail": "available", "control_state": "finished",
         "resume": {"state": "ready", "reason": null, "target": null}, "outcome": null, "complete": false,
         "steps": [
        {"id": "plan", "title": "Plan the campaign", "state": "done", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"bundle": "155ea76e…"}], "done_at": null},
        {"id": "prepare", "title": "Prepare the campaign", "state": "done", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"prepared": "109890eb…"}], "done_at": null},
        {"id": "run", "title": "Run the campaign", "state": "blocked", "optional": false, "human_only": true, "missing": [], "blocked_reason": {"code": "campaign_suspended"}, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [], "done_at": null},
        {"id": "result", "title": "Collect the result", "state": "blocked", "optional": true, "human_only": false, "missing": ["run"], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [], "done_at": null}
        ]}
      ],
      "next_cycle": {"steps": [
        {"id": "plan", "title": "Plan the campaign", "state": "done", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [{"artifact": "@manifest"}], "done_at": null},
        {"id": "prepare", "title": "Prepare the campaign", "state": "ready", "optional": false, "human_only": false, "missing": [], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [], "done_at": null},
        {"id": "run", "title": "Run the campaign", "state": "blocked", "optional": false, "human_only": true, "missing": ["prepare"], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [], "done_at": null},
        {"id": "result", "title": "Collect the result", "state": "blocked", "optional": true, "human_only": false, "missing": ["run"], "blocked_reason": null, "invalid": [], "stale_reason": null, "carried_over": [], "sources": [], "done_at": null}
      ]}
    }
  ]
}
```

(Illustrative: `connect` after check, conformance and a fixed-evaluation baseline all
passed (see [Facts](#facts-lab-project-facts4)); `evolve` with one suspended cycle —
its `run` step is blocked with `campaign_suspended`.)

**Scenario state:** `unavailable` (a fact in `requires` does not hold — `missing` names
it), `available` (requirements hold, work remains), `produced` (only for a
non-repeatable scenario: every fact in `produces` holds — the scenario's job is done,
though a stale evidence fact can move it back to `available` on the next read).

**Step state:** `done` (every output is held — a fact holds, an artifact exists, or a
matching operation outcome was recorded), `stale` (an output was held but is stale —
`stale_reason` carries `{codes, message, changes}`, the same shape as
[Staleness](#staleness)), `ready` (nothing blocks it: every step it `requires` is
`done`), `blocked` (`missing` names the requirement — a step id or a fact — that is
not yet `done`/holding, **or** `blocked_reason` names why the step cannot be done as things
are). `blocked_reason` is `null` or `{"code"}` (`schema_unusable` also carries `schema`), a
closed vocabulary; it is `null` on every other step and in every other state:

| `blocked_reason.code` | Meaning |
| --- | --- |
| `results_deleted` | The cycle's results were deleted without an export (see `delete-results` below). |
| `artifact_invalid` | A schema-checked artifact exists but fails its schema. The step's `invalid` lists `{artifact, digest, path, message}`: `artifact` is the file (relative to the materials root, `{implementation}` substituted, the same string as in the step's source), `digest` is `sha256:` of the bytes Lab checked, `path` a JSON Pointer into the file, in document order (array indices numerically), `""` for the whole file. At most 5 records **per file** in a step; files come in the order of their first output in the step, and a `(path, message)` pair that a second schema of the same file repeats is not doubled. `path: ""` is used when the file is not a regular file (a FIFO or a device is never read), not UTF-8 JSON (`NaN`, `Infinity` and `-Infinity` are not JSON), unreadable, or nested more than 200 levels deep (`nested too deeply`). `digest` is `null` only when the file was not read: `path: ""` with `not a regular file` or `the file cannot be read`. |
| `schema_unusable` | `{"code": "schema_unusable", "schema": "<name>/<n>"}`: the scenario's schema itself failed while checking (the schema's fault, not the file's); the status still answers. |
| `campaign_suspended` | The `run` step's start settled, but core's projection says the campaign is suspended (it has a resume verdict) and not terminal. |
| `campaign_state_unknown` | The start settled, but there is neither a terminal outcome nor a resume verdict to read (for example an unreadable result). |

A step has `invalid` (always present, `[]` unless `blocked_reason.code` is `artifact_invalid`).
Every step also has `optional` and `human_only` (both from the descriptor; see
[Scenario catalogue](#scenario-catalogue)). An `optional` step never keeps its scenario
or cycle from being complete and is never named in another step's `requires`.

**Step sources**, one entry per output actually held: `{"fact": id, "confirmations": [{id, author,
channel, created_at}, …]}` (a computed or confirmed fact); the list holds every live confirmation that
gives the fact to this step (the project-level one and the implementation's, the latest of each level),
ascending by `created_at` (ties by `id`), and is empty for a computed fact. Other sources:
`{"artifact": path, "digest": "sha256:…"}` (a materials-root file and the digest of the
bytes Lab looked at; `"digest": null` for an `exists` output whose file exists but cannot be
read) or `{"artifact": "@manifest"}` (the package's manifest, no `digest` key), `{"bundle": digest}` (a decide/work output of a started
repeatable cycle — it took its documents from that cycle's Bundle, not from the live
tree), `{"prepared": digest}` (a campaign PreparedExecution on the current inputs),
`{"operation": id}` (a settled campaign run) or `{"export": id, "results_state": "present" | "deleted"}`
(the campaign's export). Any held confirmation of a step's claim satisfies an attestation output,
whether it was given for the project or for the implementation. `carried_over` mirrors the fact's own `carried_over`
([Accepting changes](#accepting-changes-lab-project-acceptance1)) and is only ever
non-empty on a `done` step.

**What `/3` added (dev30).**

- Every step has a `title`, taken from the scenario descriptor (a client no longer joins it in).
- Every step has `done_at`, the moment its output **became current**, or `null`. It is set only for a
  `done` step whose every held output has a moment; a step with several outputs takes the latest.
  By source: a confirmation gives its `created_at`; a `bundle` or `prepared` link gives the link's
  `created_at`, that is the last time that input became current, so relinking moves it forward; a fact
  from an operation of this pair gives that operation's `times.last_event_at`; a fact from a task gives
  `null` (tasks record no time); a fact from another pair gives `null`; artifacts and the export give
  `null`. Facts that share an id share the latest of their moments. Nothing is guessed: no source, no
  time.
- A cycle of `evolve` has `outcome`: `{outcome, reason, champion}` (the short campaign outcome,
  the same object as `outcome` in an [operation summary](#operation-summary-lab-project-operation-list2))
  or `null` while the campaign has none, or for a cycle of another pair with no readable snapshot.
- `stale_reason.codes` is a closed contract, see [Staleness](#staleness).

**What `/4` added (dev32).**

- Every step has `blocked_reason`: `null` or `{"code": "results_deleted"}` (see
  **Step state** above and the `delete-results` rule below).
- The source shapes `fact` (with `confirmations`) and `export` (with `results_state`), as above.
- The `done_at` of a confirmed step is the `created_at` of the latest confirmation.
- Facts are `lab-project-facts/3`: a confirmed fact's `source` also carries `author` and `created_at`.
- `/3` is no longer served.

**What `/6` added (dev38).**

- Every artifact source carries the `digest` of the bytes Lab looked at, for `check: exists` as well as
  `check: schema`: `{"artifact": "SPEC.md", "digest": "sha256:…"}`. The form is that of `attests.digest`
  ([Confirmations](#confirmations-lab-project-confirmation1)). `{"artifact": "@manifest"}` has no `digest`
  key; `digest` is `null` only for an `exists` file that exists but cannot be read (for example, a path in the
  project's `exclude`: the materials revision hashes every other materials file first,
  and an unreadable one fails the request). A path is observed once per request, so every output on it,
  in every scenario, is judged by the same bytes and carries the same digest.
- Every `invalid` record names its file: `{artifact, digest, path, message}` (it was `{path, message}`). The
  cap of 5 is per file, not per step, and `digest` is the same for all records of one file.
- `lab-project-status/6`; `/5` is no longer served. `lab-project-facts/4` is unchanged.

```json
{"id": "map", "state": "blocked", "blocked_reason": {"code": "artifact_invalid"},
 "invalid": [
   {"artifact": "map.json", "digest": "sha256:9f2c…", "path": "/behaviors/0", "message": "'source' is a required property"},
   {"artifact": "routes.json", "digest": "sha256:41ab…", "path": "", "message": "the file is not UTF-8 JSON"}],
 "sources": []}
```

**What `/5` changed (dev35).**

- Every step has `optional`, `human_only` and `invalid`; see **Step state**.
- `blocked_reason.code` gains `artifact_invalid`, `schema_unusable` (with `schema`),
  `campaign_suspended` and `campaign_state_unknown`.
- The `run` rule changes. The step is `done` **only** when core's projection of the campaign
  is terminal, that is, the cycle's `outcome` is set. A start that settled while the
  campaign is suspended is `blocked` with `campaign_suspended`; a settled start with no
  projection at all is `blocked` with `campaign_state_unknown`. Before dev35 any settled
  start counted. The terminal snapshot of a campaign survives `delete-results` (Lab keeps it
  in the link's `last_seen`), so a finished campaign stays `done`.
- A cycle of `evolve` has `order` (Lab's cycle number: the position of its link among the
  campaign links of the whole project, numbered 1…N in the order they were stored, independent of the
  `implementation` filter) and `complete` (every non-optional step of the cycle is `done`).
  `order` is the same N as in `цикл N` of the
  [operation summary](#operation-summary-lab-project-operation-list2) label.
- A path of an artifact output may contain `{implementation}` and may be checked against a
  JSON Schema ([Scenario catalogue](#scenario-catalogue)).
- The catalogue is `lab-scenario/2`, `catalogue.version` is `2`; `evolve` is version `2`
  (`run` is `human_only`, `result` is `optional`).
- At dev35 facts were `lab-project-facts/4`; the retired `/4` above refers to
  project status. Current dev66 facts are `/5` ([contract](#facts-lab-project-facts5)).
- `done_at` is unchanged: its semantics are those of `/3` and `/4`, above.

**Repeatable scenarios** (`evolve`, one cycle per campaign operation) add `cycles` and
`next_cycle` instead of a flat `steps` list. `next_cycle.steps` is always the steps of
a **new** cycle (never carries over a previous cycle's Bundle or export) — what
starting one more campaign looks like right now. `cycles` lists past and current
cycles, the current one first, each `{operation_id, order, pair, current, detail,
control_state, resume, outcome, complete, steps}`. A cycle's `resume` is `{state, reason, target}`
(or `null`); `target` is `null`, or after a `target_unstable` suspension the
requirement `{requirement: "fresh_instance", reason:
"target_unstable_fresh_required", excluded: [target refs of every start]}` —
continue on a fresh target ([Resume](service-s5.md#resume-readiness)). The
requirement is stored in its own registry table, recorded at synchronisation;
a cycle whose requirement was never recorded shows `target: null`.

- `detail: "available"` means the cycle belongs to this Lab pair; `"unavailable"` marks
  a cycle of another pair. A campaign started under another state root of the same pair
  is shown as `available` too, but it is never current, and `lab status` against this
  service does not know it.
- A cycle's `steps` come only from its own snapshot: its Bundle, its PreparedExecution,
  the last recorded operation status and the recorded export of its current start. For
  a cycle of another pair nothing is read again.
- `current` marks at most one cycle of this pair and state root: the active one
  (`reserved`/`running`/`stopping`), else the one **started most recently** among those
  that can continue (`control_state` `finished` or `released` and `resume.state`
  `ready` or `cleanup_first`), else none. A resume is a new start of the same campaign,
  so an older campaign resumed after a newer one was started becomes the current cycle
  again. Cycles of this state root are listed by their latest start, newest first;
  cycles this service does not know follow them, newest link first.
- **Export tracking is per operation, not history:** the registry keeps one export id
  per campaign cycle — the export of that cycle's current start, overwritten on every
  re-export, never a list of every export ever made for it. After a resume the earlier
  export no longer counts: `result` is not done until the new start is exported.
- **After `delete-results`** Owner keeps the id of the export of that cycle's start as a mark (its
  bytes are gone and no read returns them). A cycle that was exported and then deleted keeps its
  `result` step `done`, with the source `{"export": id, "results_state": "deleted"}`. A cycle whose
  results were deleted without an export has its `result` step `blocked` with `missing: []` and
  `blocked_reason: {"code": "results_deleted"}`: a re-export refuses `results_deleted`. The same
  holds for a cycle exported and then deleted **before dev32**: dev31 sync erased its export id, so it
  has no recorded export and reads `blocked`/`results_deleted`, not `done`/`deleted`; there is no
  migration. `run`'s
  `done_at` becomes `null`, since the operation's times are gone with the results and Owner keeps no
  other timestamps. While a deletion runs or after it failed (`results_state` `deleting` or
  `delete_failed`) the export is still shown `present`. The archive cannot be read then
  (`export_unavailable`); a repeated `delete-results` finishes it. While `results_state` is `deleting`
  or `delete_failed` and the cycle has **no** export, the `result` step reads `ready`, but that `ready`
  does not mean the cycle can be exported (`export` refuses `export_unavailable`); a finished
  `delete-results` turns it into `blocked` with `blocked_reason: {"code": "results_deleted"}`. For a
  cycle of another pair, "deleted" is read from the last synchronised `results_state` of its link.

## Scenario catalogue

`GET /v1/scenarios` (`lab-scenario-list/1`) and `GET /v1/scenarios/{id}`
(`lab-scenario-entry/1`, `404 not_found` for an unknown id) read the catalogue baked
into the installed wheel — no Lab home, no project, no pair (ADR-0017 p. 8 partially
replaces ADR-0012's "no service route for guides"). Offline, without a socket:
`lab scenarios list [--json]`, `lab scenarios show ID [--json]`, `lab scenarios path`
(the `scenarios/` directory) — `lab scenarios` rejects `--socket`, `--home` and
`--timeout`, unlike every other client command. In Python: `evolution_lab.guides.scenarios()`.

A scenario is what `lab-project-status/6` walks: an `id`, a `version`, the facts it
`requires` and `produces`, whether it is `repeatable`, and an ordered list of `steps`
— each with a `kind` (`decide`, `work`, `operation`, `confirm`), the outputs that mark
it done, and a pointer into its own `SKILL.md`. Three scenarios ship today:

| Scenario | Subject | Requires | Produces | Repeatable |
| --- | --- | --- | --- | --- |
| `connect` | implementation | `implementation.available` | `connected` | no |
| `evolve` | implementation | `connected` | `campaigns` | yes, one cycle per campaign operation |
| `restructure` | implementation | `implementation.ported` | `restructure.port_accepted` | no |

**Layout.** Each scenario is a directory `guides/scenarios/<id>/` with `SKILL.md`
and `scenario.json` (the descriptor), plus `schemas/<name>-<n>.json` for each declared
schema; `catalogue.json` at
`guides/scenarios/` lists every scenario's `id`, `version` and digest plus its own
`catalogue_version` and digest — the same digest-catalogue shape as the requirements
catalogue (ADR-0012). The catalogue shipped in this wheel:
`catalogue_version: 12`, digest
`bbb65a2b34e85002407027fffe5cba9426fa25eb2a058a6ad8350bb9239dfecf` (`connect` version 8,
`create-skill` version 1, `evolve` version 2, `restructure` version 2). Lab loads `lab-scenario/2` only; `lab-scenario-catalogue/1` keeps its
keys, and only `catalogue_version` moved (1 → 2 with `/2`, 2 → 3 with `restructure`, 3 → 4 with the answers file of `connect`, 4 → 5 with `restructure` 2, 5 → 6 with `connect` 3 (requirements catalogue v8), 6 → 7 with `connect` 4 (requirements catalogue v9), 7 → 8 with `connect` 5 (requirements catalogue v10, `/15`-only admission), 8 → 9 with `connect` 6 (requirements catalogue v11, the optional `/16` item `campaign.independent_acceptance`), 9 → 10 with `connect` 7 (requirements catalogue v12, corrected optional `campaign.budget` explanation), 10 → 11 with `connect` 8 (requirements catalogue v13, `run.failures`), 11 → 12 with `create-skill` 1).

The requirements-v13 data revision changes linked prior connect confirmations
to `catalogue_changed`; saved user answers/confirmations are not migrated.

**Descriptor.** `lab-scenario/2` is a closed object: `schema`, `id`, `version`, `title`,
`skill`, `subject` (`implementation` or `project`), `requires`, `produces`, `repeatable`,
`steps`, `digest`, and the optional `schemas` (default `{}`, see
[Scenario schemas](#scenario-schemas)). A step is `id`, `title`, `kind`, `human_only`,
`requires`, `outputs`, `skill`, and the optional `optional` (default `false`).

- **`human_only`** is allowed on `operation` and `confirm` steps only. It is declarative:
  it marks a step that needs the human's confirmation at `ask`. Lab shows it in the
  status and workspace `AGENTS.md`. An assistant can run the requested step at `yolo`.
- **`check: "schema"`** artifact outputs are refused in a repeatable scenario
  (`repeatable` not `null`): a started cycle takes its documents from the cycle's Bundle and
  would not check them against the schema (v1; user decision 2026-10-01).
- **`optional`** steps do not count for `complete` (status), may not be named in another
  step's `requires`, and a fact in the scenario's `produces` may not come only from
  optional steps. The sources of an attested claim are its `attestation` outputs; those of a
  computed fact are the outputs the status reads it from: `@package` (`package.valid`),
  `check/passed` (`check.passed`), `conformance/passed` (`conformance.passed`),
  `evaluation/measured` (`baseline.measured`), `campaign/terminal` (`campaigns`), and for
  `connected` each of the three operation facts it needs. A produced fact some steps
  establish, but only optional ones (for `connected`: any of its three), refuses the
  catalogue.
- **`{implementation}`** may appear in an artifact or attestation `path` only when the
  scenario's `subject` is `implementation`; otherwise the catalogue is refused with
  `implementation_placeholder_without_subject`. Status replaces it with the implementation
  id; the substituted path must stay inside the materials root (relative, no `..`, no
  `@` name), else the output is not considered. A client that confirms such a step passes
  the **already substituted** path: `lab project confirm --path <path>`; Lab does not
  substitute on a confirmation.
- A catalogue error (a bad descriptor or schema, including
  `implementation_placeholder_without_subject`) is raised when the catalogue is loaded, with
  a message that names the scenario; it is never a status answer.

### Scenario schemas

An artifact output may be checked against a JSON Schema instead of only for existence:
`{"artifact": {"path": "<path>", "check": "schema", "schema": "<name>/<n>"}}`. The step is
`done` only when the file exists, parses as UTF-8 JSON and the schema accepts it;
a missing file is "not done" (like `exists`), an invalid one is `blocked` with
`artifact_invalid`, a schema that fails to run gives `schema_unusable`
([Status](#status-lab-project-status6)).

- **Declaration and file.** The descriptor's `schemas` maps `"<name>/<n>"` (`<name>`
  `[a-z][a-z0-9-]*`, `<n>` a positive integer) to the file `schemas/<name>-<n>.json` in the
  scenario's directory (`guides/scenarios/<id>/schemas/`).
- **Dialect and self-containment (ADR-0020).** Draft 2020-12 only (`$schema`, when given,
  must be `https://json-schema.org/draft/2020-12/schema`). A schema is self-contained:
  every `$ref` must start with `#` and **resolve inside the schema at load time**;
  `$dynamicRef`, `$dynamicAnchor`, `$recursiveRef` and `$recursiveAnchor` are forbidden;
  `$id` is allowed **only at the root** (a nested `$id`, including a fragment one, is an
  error: name sub-schemas with `$anchor`). A `$ref` must land on a subschema: a target inside
  data (`const`, `enum`, `default`, `examples`, `dependentRequired`), at the name level of
  `properties`/`$defs`/… or on an array is an error (`$ref into data`). Nothing is retrieved from disk or the network:
  the resource registry is empty. `format` is not asserted.
- **Digest.** A scenario's digest is the sha-256 of the canonical JSON of the descriptor
  without `digest`, plus, when there are schemas, `schema_documents: {name: canonical
  schema document}`. A schema change is therefore a scenario change: a new `version`, a
  new digest and a new catalogue digest. A confirmation linked with
  `--link catalogue:scenarios` goes stale with `catalogue_changed`; confirmations over a
  schema-checked artifact should carry that link, so that a schema change asks for a new
  confirmation.
- **Reading a schema.** `lab scenarios schema <scenario-id> <name>/<n>` (offline, like the
  other `lab scenarios` commands) and `GET /v1/scenarios/{id}/schemas/{name}/{n}` (for
  example `…/schemas/plan/1`) answer the file as shipped, byte for byte, with media type
  `application/schema+json`. An id or schema that is not declared is `404 not_found`.
  The route is also on the panel's allowlist.
- `connect` declares `spec-answers/1` (the answers file `spec.answers.json`, document schema
  `connect-spec-answers/1`). It is generated from the requirements catalogue by
  `evolution_lab.guides.answers` (`python -m evolution_lab.guides.answers` rewrites the file), so a new
  catalogue version changes the schema and with it the digests of `connect` and of the scenario catalogue.
  `restructure` declares `behavior-map/2`, `goal/2`, `port-spec/2`, `scope/1` and `verify/2` (see its skill); `evolve` declares none.

**Naming.** A scenario's skill is `evolution-lab-scenario-<id>` (`evolution-lab-scenario-connect`,
`evolution-lab-scenario-evolve`) — the ordinary guides keep the plain
`evolution-lab-<directory>` rule (`evolution-lab-diagnostics`, `evolution-lab-preparation`, …).

**Versions.** Changing a descriptor's steps, facts or skill anchors is a new
`scenario.json` with a bumped `version` and a recomputed digest; the catalogue's own
`catalogue_version` and digest move with it. A running status payload always reports
the version of the catalogue actually installed — there is no cross-version
compatibility to preserve, since the catalogue is wheel data, versioned with Lab
itself.

## Commands in scenario skills

The `connect` and `evolve` skills together name 37 `lab` commands:

| Scenario | Command |
| --- | --- |
| connect | `lab project status <project> --implementation <implementation> --scenario connect` |
| connect | `lab guides path` |
| connect | `lab guides requirements --json` |
| connect | `lab scenarios schema connect spec-answers/1` |
| connect | `lab project port <project> --from <implementation> --template <template-source>` |
| connect | `lab project confirm <project> --implementation <implementation> --claim spec.confirmed --path SPEC.md --author <name> --link catalogue:requirements --link file:spec.answers.json` |
| connect | `lab project confirmations <project>` |
| connect | `lab project impl set-package <project> <implementation> <entry>` |
| connect | `lab project facts <project> --implementation <implementation>` |
| connect | `lab project bundle <project> --implementation <implementation>` |
| connect | `lab connect <bundle-digest>` |
| connect | `lab project target <project> --implementation <implementation> --bundle <bundle-digest>` |
| connect | `lab job status <task-id> --generation <owner-generation>` |
| connect | `lab check <connection-id> --target <target-id> --generation <owner-generation> --wait` |
| connect | `lab conformance <connection-id> --target <target-id> --generation <owner-generation> --wait` |
| connect | `lab project prepare <project> --implementation <implementation> --request <evaluation-request.json>` |
| connect | `lab project target <project> --implementation <implementation> --bundle <bundle-digest>` |
| connect | `lab job status <task-id> --generation <owner-generation>` |
| connect | `lab project start <project> --implementation <implementation> --prepared <prepared-digest> --target <target-id>` |
| connect | `lab status <operation-id>` |
| connect | `lab project accept <project> --implementation <implementation> --reason <reason>` |
| evolve | `lab project status <project> --implementation <implementation> --scenario evolve` |
| evolve | `lab guides path` |
| evolve | `lab project facts <project> --implementation <implementation>` |
| evolve | `lab project bundle <project> --implementation <implementation>` |
| evolve | `lab project prepare <project> --implementation <implementation> --request <campaign-request.json>` |
| evolve | `lab project target <project> --implementation <implementation> --bundle <bundle-digest>` |
| evolve | `lab job status <task-id> --generation <owner-generation>` |
| evolve | `lab project start <project> --implementation <implementation> --prepared <prepared-digest> --target <target-id>` |
| evolve | `lab status <operation-id>` |
| evolve | `lab events <operation-id>` |
| evolve | `lab stop <operation-id>` |
| evolve | `lab resume <operation-id> --target <target-id> --generation <owner-generation>` |
| evolve | `lab report <operation-id>` |
| evolve | `lab diff <operation-id>` |
| evolve | `lab export <operation-id> --wait` |
| evolve | `lab export-content <operation-id> <export-id> --output /absolute/path/change-set.zip` |

## Target entry file `lab-target-entry/1`

An implementation with no `package_entry` can start a live target from
`lab-target-entry.json` in the root of its place. `POST /projects/{id}/targets`
(`lab project target`) chooses the source: the package if the implementation
has one (its `launch.json`, unchanged); otherwise the entry file, which
produces a Bundle of kind `probe`; otherwise `target_undeclared`. The response
`lab-project-target-result/1` is unchanged; the source is visible in the probe
session (`target.source`: `package | entry`) and in the Bundle's `source_kind`.

The key set is closed (an unknown key is `target_entry_invalid`, `detail`
"unknown key"):

| Key | | Meaning |
| --- | --- | --- |
| `schema` | required | `lab-target-entry/1` |
| `agent` | required | `{module, attribute, import_root}` as in `lab-target-launch/2`; `import_root` is a relative path inside the place, without `..` |
| `program_paths` | required | non-empty list of relative paths inside the place, without `..` |
| `environment` | required | the target's environment: `{"kind": "service"}` or `{executable, wheel_paths, runtime_files}` with absolute paths **outside** the place (a path inside it is `target_entry_invalid`, `detail` `environment`) |
| `env` | required | non-secret values |
| `secret_names` | required | names only, unique, not also in `env` |
| `limits` | optional | `{max_instances 2, max_concurrent_sessions 1, max_input_bytes 1 MiB, max_output_bytes 64 KiB}`; positive integers |
| `startup_timeout_seconds` | optional | default 30 |
| `stop_grace_seconds` | optional | default 5 |

Lab synthesizes a `lab-target-launch/2` and a policy point from it
(`target_id` `lab-probe-<project>-<implementation>`, `seed_mode` `none`, real
digests). A probe Bundle only raises a target for probes. Preparation refuses
prepared execution of it (`prepare` of a campaign or an evaluation:
`artifacts_invalid`); a campaign or evaluation start, a resume or conformance on
its target is `bundle_probe_only` (409). A client Bundle request
`lab-service-bundle-request/2` does not accept `source.kind: "probe"`.
Editing the file changes the place's digest like any code change. The
guide is [target-connector](../../src/evolution_lab/guides/target-connector/SKILL.md).

## Probe sessions

A probe session talks to an agent on a live, `ready` target of an
implementation, before any metric exists. The panel does **not** serve these
routes (PDR-0017: read only); the service, the CLI and
`ProjectSession.probe_*` do. Available from Lab `0.0.3.dev37`.

**Create.** `lab-service-project-probe-request/1`:

```json
{"schema": "lab-service-project-probe-request/1", "implementation": "agent",
 "ceiling": {"max_turns": 10, "max_tokens": 20000, "max_cost": "1.00"},
 "budget": {"ledger_path": "/abs/ledger.sqlite3", "roles": ["agent"], "currency": "RUB"},
 "turn_timeout_seconds": 120, "seed": null}
```

All keys are present; `budget`, `seed` and `turn_timeout_seconds` may be
`null` (the timeout then defaults to 120 s; at most 600). The ceiling is
mandatory: `max_turns` 1 to 1000; `max_tokens` optional; `max_cost` (decimal
string) only with `budget`. `seed` is allowed only for a target in `exact`
seed mode (else `invalid_request`). The response is 201 `lab-probe-session/1`.

**Turn.** `lab-service-probe-turn-request/1` `{schema, content}`: non-empty
text, at most 1 MiB of UTF-8. The call is synchronous and answers 200
`lab-probe-turn/1`. Turns of all sessions on one target run one at a time.
A failed turn is recorded with `outcome: target_failed` and a `failure`, and
closes the session. A turn that times out (`turn_timeout`) leaves the target's
state unknown: the release never settles, so the session closes with
`closed_reason: release_failed` and the target needs a stop. A definitive
failure (the target answered with an error, `protocol_error`, e.g. a message
above the target's `max_input_bytes`) closes it with
`closed_reason: turn_failed` when the release settles, and the target stays
usable.

**Forms.** All three are closed, `/1`. One ceiling shape everywhere:
`{turns: {used, max}, tokens: {used, max} | null, cost: {used, max, currency} | null}`
(`turns` always present).

- `lab-probe-turn/1`: `session_id`, `turn_id` (`<session>/<n>`), `started_at`,
  `finished_at`, `latency_ms`, `request {content}`, `answer` (text of the last
  `assistant_message` event, else `null`), `steps [{index, type, data}]` (core's
  events in order, unrenamed), `outcome`, `reason`, `agent_outcome`, `usage`
  (`{prompt, completion, cached, total}` or `null`), `cost` (`null` or
  `{amount, currency, attribution: "ledger_window", upper_bound}`), `truncated`,
  `failure` (`null` or `{code}`: `turn_timeout`, `target_unreachable`,
  `protocol_error`, `interrupted`; then `outcome: target_failed`, `answer: null`,
  `steps: []`), `ceiling` (state after the turn).
- `agent_outcome`: `answered | off_topic | handoff | gave_up | error |
  infrastructure | unspecified`. A template declares the first four with
  `reason {category: "agent_outcome", code}` on an `ok` turn; `error` is
  `candidate_failed`; `infrastructure` is `target_failed` or a turn Lab did not
  bring to an answer; `unspecified` is `ok` without such a reason. It means
  nothing to campaigns and evaluation.
- `lab-probe-session/1`: `id`, `project`, `implementation`, `state`
  (`open | closed`), `closed_reason` (`reset | closed | turn_failed |
  target_gone | release_failed | null`), `reset_of`, `created_at`, `closed_at`,
  `target {id, owner_generation, boot_id, bundle_digest, launch_digest, source,
  seed_mode}`, `seed {requested, effective}`, `ceiling`, `budget` (`null` or
  `{ledger_path, roles, currency}`), `tokens_incomplete`, `cost_unavailable`,
  `turn_timeout_seconds`, `turns` (forms `lab-probe-turn/1`).
- `lab-probe-session-list/1`: `{schema, sessions}`, sessions without `turns`.

**Ceiling.** Checked before each turn and spanning the whole chain of sessions
linked by `reset_of`, so a reset does not zero it. A turn cannot be stopped in
the middle, so one may overshoot a token or cost ceiling; `ceiling` shows it.
Money is counted only when a gateway is declared
([Budget](../../../docs/guide/budget.md#probe-sessions)).

**Lifecycle.** An open session holds a target instance and blocks `check`,
`conformance` and operations on that target (`probe_sessions_open`). `reset`
closes the session and opens a new one with the same ceiling and gateway
(201); `DELETE` closes it. Stopping the target closes its sessions
(`closed_reason: target_gone`); so does a service restart. A session is then
read-only; its history stays. An unsettled release gives
`closed_reason: release_failed` and the target `cleanup_required`: only a target
stop helps. A reset whose release does not settle answers `cleanup_required`
(409): the old session is `release_failed` and no new one opens. A service
shutdown waits for a running turn at most `shutdown_wait_seconds`; a turn still
queued is refused `service_stopping`, and a turn the shutdown interrupts is
recorded at the next start with `failure: {code: "interrupted"}` (session
`target_gone`). If `probes.sqlite3` cannot be opened (another version, a
damaged file) the service still starts and the probe routes answer
`history_unavailable` (409) until the file is fixed. History is in
`LAB_HOME/probes.sqlite3` (written by the service only, mode 0700 directory);
message text never goes to service logs.

**Mutation keys.** Create, turn, reset and close are keyed like other
mutations, in their own class `probe` with 1,000 records per generation: a
repeated key returns the same answer rather than a second paid turn.

**CLI.** `lab probe start|say|history|list|reset|close`:

```sh
lab probe start P I --max-turns 10 [--max-tokens N] [--max-cost X \
  --budget-ledger PATH --budget-role R ... --budget-currency RUB|USD] [--turn-timeout S] [--seed N]
lab probe say SESSION "text"     # "-" reads stdin
lab probe history SESSION
lab probe list P [--implementation I]
lab probe reset SESSION
lab probe close SESSION
```

`say` waits for the turn: a `--timeout` below 610 s is raised to 610 s, with a
notice on stderr. `--json` prints the form as is.

## Ports (`lab project port`)

A **port** is a new implementation of a project that is built from a **template** (a
git repository such as a framework skeleton) and registered as `ported_from` an existing
implementation. Lab creates the port's own git repository from the template's tracked
files; porting the agent's code into it is your (or your assistant's) work. Lab knows no
template by name: a template is always a path or a URL you pass.

`POST /v1/projects/{id}/ports` (CLI `lab project port ID --from SOURCE --template
SRC[@REF] [--id PORT_ID] [--into DIR]`), a keyed mutation of class `project`. Request
`lab-service-project-port-request/1`:

```json
{"schema": "lab-service-project-port-request/1", "from": "agent",
 "template": "https://git.example.com/team/sylph.git", "ref": "v1", "id": null, "into": null}
```

| Field | Meaning |
| --- | --- |
| `from` | The source implementation: active, in this project. |
| `template` | An absolute local path (the CLI makes a relative one absolute; the top level of a repository, or a bare repository) or a git URL (`https://`, `ssh://`, `git://`, `file://`, `user@host:path`). |
| `ref` | A branch, tag or commit of the template, or `null` for `HEAD`. The CLI takes it from `--template <src>[@<ref>]`: the ref follows the last `@` after the source's path begins, so `user@host` and URL credentials are never read as a ref; a **local path that itself contains `@`** names its ref explicitly (`/srv/x@y@HEAD`). |
| `id` | The port's implementation id (`^[a-z][a-z0-9-]{1,39}$`), or `null`: `<from>-port`, then `<from>-port-2`, `-3`, … The default must fit the same 40-character grammar; for a long source id it does not (`invalid_id`), so pass `--id`. |
| `into` | An absolute destination directory, or `null` for the default `LAB_HOME/ports/<project>/<port-id>`. |

The answer is the updated project, `lab-project/2`; the new implementation has
`origin: {"kind": "ported_from", "implementation": "<from>"}`, an absolute `place`, no
package entry, and `port: {template, ref, commit, created_at}`. Set its package afterwards
(`lab project impl set-package`).

**Checks, before anything is written, in this order:** the project is `ready`
(`project_not_ready`); the request is well formed (`invalid_request`: a malformed field,
an empty value or one starting with `-`, a template that is neither a URL nor an absolute
path, a relative or NUL-containing `into`); the source exists (`implementation_not_found`)
and is active (`implementation_archived`); the port id is valid (`invalid_id`) and unused
(`id_taken`; archived ids stay taken); `git` is available (`git_unavailable`); the
destination is not inside the project's working copy (`port_into_working_copy`) and, with
`--into`, not inside the Lab home (`port_into_lab_home`); the destination does not exist or
is an empty directory (`port_destination_exists`). The template is read afterwards:
`template_not_git`, `template_ref_not_found`, `template_fetch_failed`.

**Destination.** The default is `LAB_HOME/ports/<project>/<port-id>`. `--into` is an absolute
path **outside the working copy and outside the Lab home**; it must not exist or must be an
empty directory. The staging directory is made next to the destination (a sibling
`.staging-<name>-<random>`), so the destination's parent must be writable.

**Template.**

- A **local path** must be the top level of a git repository, or a bare repository (a
  directory inside another repository is `template_not_git`). The revision is resolved
  with `<ref>^{commit}`.
- A **URL** is fetched shallow (`--depth 1`, the one ref) into `LAB_HOME/tmp/port-<random>/`,
  removed afterwards. A ref that is a full commit hash is fetched by hash. Every network git call
  (`ls-remote`, `fetch`) runs with `-c credential.helper=`, `GIT_TERMINAL_PROMPT=0` and Lab's usual ssh
  rule (see [Creating a project](#creating-a-project)), so no credential helper is asked
  or fed.
- The snapshot is the **tracked files at the commit** (`git archive`): no history, no
  remote, no ignored files; the template's own symlinks are kept, names that escape the
  directory are refused. Being `git archive`, it honours the template's `.gitattributes`
  (`export-ignore` files are left out, `export-subst` placeholders are expanded), and a
  template that keeps files in Git LFS ports as LFS pointer files, not their contents.

**The repository.** In the staging directory Lab runs `git init`, adds those files and makes
**one** commit `Lab: port of <source> from <template>@<commit>` (the template with
credentials replaced by `***`; `<commit>` the full template commit), then the tag
`lab/port/created` on it. The commit uses your git identity (`user.name` and `user.email`);
when either is not configured Lab uses `Evolution Lab <lab@evolution-lab.invalid>` for this
command only and writes no git config. No remote is set and nothing is pushed: delivering
the port is your action.

**Publication.** Slow work (fetch, snapshot, commit) runs **outside** the session's mutation
lock and off the service's IO worker. Under the lock Lab repeats the checks, renames the
staging directory to the destination and writes the implementation and its `ports` row in
**one registry transaction** (both or neither). If the transaction does not commit, the
renamed directory is removed again (an empty destination directory you had made is left
empty). A failure anywhere before that leaves nothing, and `port_failed` / `template_*`
report it.

**Leftovers and cleanup.**

- Opening the registry for writing (service start) removes only Lab's own leftovers: a
  `LAB_HOME/tmp/port-*` or `LAB_HOME/ports/*/.staging-*` directory **that carries Lab's mark**,
  an empty file `<directory>.lab-port-leftover` next to it (written right after the directory
  is created, removed when the fetch ends or after the staging is renamed into place, so a
  published port never carries it). A name alone is never enough. A marked directory is still
  kept when it is, contains or lies inside a registered place (any implementation or working
  copy, active or archived) or the service's state root; symlinks are never followed. A mark
  whose directory is gone is removed. A staging directory next to an `--into` destination is
  not tracked: after a crash, remove it (and its mark) yourself.
- A crash between the rename and the commit can leave the destination directory without a
  registered implementation; the next `port` to it answers `port_destination_exists` and
  you remove the directory.
- Lab **never deletes a port directory**, also when the implementation is archived
  (`archive_implementation` keeps the directory and the `ports` row).

**Credentials.** URL credentials are never stored (`ports`, the project record), committed,
returned or logged: the redacted form `https://***@host/…` is what the `ports` row, the
commit message and the answer carry, git's argv is never logged and its stderr tail is
redacted before it reaches the service log.

The fact [`implementation.ported`](#facts-lab-project-facts4) holds for the new
implementation while its source stays registered.

## Sync (`lab project sync`)

Available from the release that carries PDR-0024. A project made with `--clone` freezes at the commit it
was cloned at; `lab project sync` brings in what the team repository gained since, in two steps: **check**
(fetch and report; the working copy does not change) and **apply** (merge exactly the commit the check
showed into `lab/<id>`). Lab never pushes, and it fetches from the clone's source only here (a port fetches templates).

```bash
lab --socket $S --json project sync acq                 # check: fetch, then report
lab --socket $S --json project sync acq --rev release   # check against another branch, tag or commit
lab --socket $S --json project sync acq --apply 3f9c0a1d…   # apply: the full 40-hex sha the check printed
```

Without `--json` the check prints a short report: one line per commit, one per file (`A`, `M`, `D`,
`old -> new` for a rename), the `dirty` entries and the ready command `next: lab project sync <id> --apply <sha>`
(not printed when nothing is new or the copy is dirty).

**What is synced.** Only a Lab clone in its place: the project is `ready`, `working_copy.created_by_lab`
is true, `source` is not `null` **and** `working_copy.path` is `clones/<id>/` of the Lab home. A
directory you registered with `--path`, a clone whose working copy you moved with `lab project set
--working-copy`, and a [port](#ports-lab-project-port) (its own repository without a remote) are
`not_a_clone`.

### Check `lab-project-sync-check/1`

`POST /v1/projects/{id}/sync/check`, body `lab-service-project-sync-check-request/1` `{schema, rev}`
(`rev` is a string or `null`). The check runs outside the session's mutation lock (a fetch can be long).

1. `git fetch --quiet --prune --tags --force origin` in `clones/<id>/`: the remote the clone recorded
   (in the clone's `.git/config`; URL credentials are not stored in the project record and never appear in
   an answer), with the clone's environment (`GIT_TERMINAL_PROMPT=0`, `-c credential.helper=`, ssh
   `BatchMode=yes` unless you set your own ssh). Only `refs/remotes/origin/*` and tags change; a tag the
   source moved follows it, and `--prune` does not delete local tags. Failure: `sync_fetch_failed` (git's
   stderr tail goes to the service log, credentials redacted).
2. `upstream`: without `rev`, `refs/remotes/origin/HEAD` (Lab does not run `git remote set-head`; if the
   source has none, `revision_not_found` asks for `--rev`). With `rev`, the clone's rules:
   `origin/<rev>`, then the tag `<rev>`, then `<rev>` as a commit. A `rev` that starts with `-` is
   `invalid_request` before git runs.
3. `source.base_revision` must be an ancestor of `upstream` (`history_diverged` otherwise: the source
   rewrote history; make a new project, or repair it by hand).

```json
{
  "schema": "lab-project-sync-check/1",
  "project": "acq",
  "base_revision": "<sha>",
  "upstream": {"sha": "<sha>", "ref": "origin/main"},
  "mode": "merge",
  "commits": [{"sha": "<sha>", "author": "Name", "date": "2026-10-08T12:00:00+03:00", "subject": "New skill"}],
  "commits_truncated": false,
  "files": [{"path": "skills/new.md", "status": "A"}, {"path": "a.py", "status": "R", "old_path": "b.py"}],
  "files_truncated": false,
  "dirty": []
}
```

| Field | Meaning |
| --- | --- |
| `upstream.ref` | How `upstream` resolved: `origin/<branch>`, `tag:<tag>`, or `null` for a bare commit. |
| `mode` | `up_to_date` (`upstream` is `base_revision` or already an ancestor of `HEAD`), `fast_forward` (`HEAD` is an ancestor of `upstream`: `lab/<id>` has no commits of its own) or `merge` (it has, for example the connection package). |
| `commits` | `base_revision..upstream`, newest first, at most 200; `commits_truncated` says more exist. |
| `files` | `git diff --name-status -M base_revision upstream`; status `A`, `M`, `D` or `R` (any other git status is reported as `M`; `old_path` only for `R`); at most 1000, with `files_truncated`. |
| `dirty` | Tracked files with changes in the working tree or index (at most 50), `{"path"}` each; an unfinished merge or rebase is one entry `{"path": null, "state": "merge_in_progress"}` (or `rebase_in_progress`). The check does not refuse on `dirty`; apply does. |

### Apply `lab-project-sync-result/1`

`POST /v1/projects/{id}/sync`, body `lab-service-project-sync-request/1` `{schema, upstream}` (`upstream`
is a full 40-hex sha, otherwise `invalid_request`). Apply does **not** fetch again: it merges exactly that
commit from the local objects, so you merge what you read. Without a prior check the commit is not there
(`upstream_changed`). The commit must be reachable from the source's branches or tags, or be (an ancestor
of) a commit a check showed: a check records what it shows in Lab's own refs `refs/lab/sync-checked/<sha>`
in the clone, so a commit you approved stays applicable (and a `sync_not_recorded` retry works) after the
source deleted its branch. A commit only the clone has (an assistant's commit on `lab/<id>`) is
`upstream_changed`. A registry opened read-only (no home lock) refuses sync with `home_busy`, before any git.

Under the project's mutation lock (like a port's publication) and a per-project lock on the working copy,
Lab re-checks the project preconditions, then:

1. the copy is on `source.branch` (`lab/<id>`), else `branch_mismatch`;
2. `base_revision` is an ancestor of `upstream`, else `history_diverged`;
3. tracked files are unmodified and no merge or rebase is unfinished, else `working_copy_dirty`.
   Untracked files do not matter unless git would overwrite one: then `merge_conflict` naming the
   paths. **Ignored** local files git overwrites silently, and a real merge ignores
   `--no-overwrite-ignore`; so Lab refuses `merge_conflict` itself, before any merge, when a path the
   source adds is an ignored local file or directory (also through a parent directory);
4. if `upstream` is already an ancestor of `HEAD`: `mode: "up_to_date"`, nothing is merged, and
   `base_revision` moves to `upstream`;
5. if `HEAD` is an ancestor of `upstream`: `git merge --ff-only <sha>`; otherwise `git merge --no-ff
   --no-edit -m "lab: sync <sha12>" <sha>` with Lab's identity (`Evolution Lab <lab@evolution-lab.invalid>`).

Merge policy is **not** read from your git configuration: both forms pass an explicit mode,
`--no-overwrite-ignore --no-verify --no-gpg-sign --no-autostash`, `-c core.hooksPath=/dev/null`,
`-c merge.ff=true` and `-c branch.<branch>.mergeOptions=` (so `merge.ff=only`, a signing setting, a
`pre-merge-commit` hook or `-s ours` on the branch change nothing). Lab passes no `-s` or `-X`. The merge
and its abort also run without your user and system git configuration (`GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM=1`, `core.attributesFile=/dev/null`), so a `merge.default` or merge driver there
cannot turn a conflict into a quiet "success". If the configuration the merge will read (local, worktree and `[include]`/`[includeIf]` files) names a merge driver or
`merge.default`, Lab refuses `merge_failed` ("custom merge driver configured in the working copy")
before merging. The fetch keeps your configuration (ssh and credentials may live there).

A merge that fails, or that is interrupted by a timeout or a git error, is undone through one recovery path: on a conflict, `git merge --abort`, then `merge_conflict` with up to 50
paths in the message; on any other failure `merge_failed`. Either way Lab then checks that `HEAD` and the
tracked files are as before; if not, `merge_failed` says the copy is unfinished and a person must
repair it (the project stays `ready`; the next check shows it as `dirty`).

On success `source.base_revision` becomes `upstream`, written to the registry after the merge. Git is
the source of truth: if that write fails, `sync_not_recorded` (merged, `base_revision` not recorded; also when reading `HEAD` fails right after a successful merge);
repeat `--apply` with the same sha with a new mutation key (a key repeats its stored result). The repeat
sees `upstream` in `HEAD` (`up_to_date`) and only records `base_revision`.

```json
{"schema": "lab-project-sync-result/1", "mode": "merge", "merged": "<sha>", "head": "<sha>", "project": {"schema": "lab-project/2"}}
```

`mode` is `fast_forward`, `merge` or `up_to_date`; `merged` is the commit you named, `head` is `HEAD`
afterwards, `project` is the updated `lab-project/2` (abbreviated above).

An admitted check or apply runs to its end when the caller is cancelled: git is not interrupted and the
locks are not released early; a cancelled caller waits for the end and only then gets the cancellation.

### What sync does not touch

Running operations, targets and probes work on copies inside their Bundles (ADR-0003, ADR-0017); sync
neither waits for nor stops them. Facts and confirmations are not edited: after a merge the digests of
the places and the inventory change, and they go stale through the usual reasons (`place_changed`,
`inputs_changed`, `digest_changed`); `lab project status` shows what to re-check. The registry schema and the
record form do not change (`base_revision` and `record_version` do). Out of scope (PDR-0024): push, delivery to the team
repository, background or automatic fetch, predicting conflicts, resolving them, rebase, picking some
commits, syncing a `--path` directory. The panel does not forward these routes.

## Assistant workspace `lab-workspace/1`

`POST /v1/projects/{id}/workspace` (CLI `lab project open ID [--autonomy ask|yolo] [--print]`, a keyed
mutation, class `project`) generates the project directory an assistant works
from: `<home>/workspaces/<id>/`, rebuilt **whole** on every call — anything
written there by hand is lost on the next `open`. An archived project answers
`archived`.

The closed JSON request `lab-service-project-workspace-request/1` has exactly
`schema` and `autonomy` with level `ask` or `yolo`. An empty body still selects
`ask`, the CLI default. The level lives in the generated instructions and
client arguments.

| Entry | Content |
| --- | --- |
| `AGENTS.md` | Generated: the implementations (an implementation outside the working copy is marked `linked as impl/<id>`), `lab project status <id>` as the starting command, a "Lab tools" section (with the `mcp` extra) or a "Lab tools are not configured" notice (without), the scenario catalogue (each scenario lists its `human_only` steps), and the selected autonomy rule. Commands in it are absolute and carry `--socket`. Its own header says edits are lost. |
| `CLAUDE.md` | `@AGENTS.md` (one line). |
| `.claude/skills/<name>/`, `.agents/skills/<name>/` | Copies of the ten reference skills plus the four scenario skills (`evolution-lab-scenario-connect`, `evolution-lab-scenario-create-skill`, `evolution-lab-scenario-evolve`, `evolution-lab-scenario-restructure`), same trees under both roots. Relative links are adjusted to the workspace layout. |
| `.claude/guides/`, `.agents/guides/` | Supporting Owner and service guides linked from the skills. |
| `work` | A symlink to the working copy. |
| `materials` | A symlink to the materials root inside it. |
| `impl/<id>` | Present only when an **active** implementation has an absolute `place` outside the working copy (a [port](#ports-lab-project-port), or one registered with an outside place): a symlink to that place, one per implementation. Without such implementations there is no `impl/`. |
| `.mcp.json` | Claude Code: `{"mcpServers": {"evolution-lab": {"command", "args"}}}`. Present only when `mcp` is installed in the service process in the extra's range `>=2.3,<3`. |
| `.codex/config.toml` | Codex: `[mcp_servers.evolution-lab]` with the same `command` and `args`. Present only with the extra. |
| `.qwen/settings.json` | Qwen Code: the same JSON as `.mcp.json`. Present only with the extra. |
| `.gigacode/settings.json` | GigaCode CLI: the same JSON as `.mcp.json`. Present only with the extra. |
| `.lab-workspace.json` | `lab-workspace/1`: `{schema, project, pair, socket, generated_at, catalogues: {scenarios, requirements}}` — the two catalogue digests the workspace was generated against. |

The four client files declare the server `evolution-lab`: `command` is the
service process's `sys.executable` (not resolved through symlinks) and `args`
are `["-E", "-P", "-m", "evolution_lab.cli", "mcp", "--socket", "<socket>", "--autonomy", "<level>"]`.
They are byte-identical for the same service, project, installation and level. Without
the extra none of them is written, and `AGENTS.md` carries a "Lab tools are not
configured" notice instead of the "Lab tools" section. Lab writes no trust or
approval settings.

Every command written into `AGENTS.md` is absolute and carries `--socket
<path>`, naming the exact socket this service listens on: the service's
interpreter with `-E -P -m evolution_lab.cli --socket <path>`, the same prefix the MCP server uses. This holds with and without the
extra.
Lab never writes into the working copy or the materials root building the
workspace — only symlinks are made under `work`, `materials` and `impl/`.

Response: `{"schema": "lab-project-workspace/1", "path":
"<home>/workspaces/<id>"}`. Without `--json` the CLI prints `path`;
`--print` prints the regenerated `AGENTS.md`, including the selected autonomy, instead — for an assistant harness that does not
read `AGENTS.md` on its own. `--print` has no effect together with `--json`:
the response body is printed either way.

## The free demo project

`POST /v1/projects/demo` (CLI `lab project demo [ID]`, request
`lab-service-project-demo-request/1` `{schema, id: <id> | null}` — an omitted
id gets the same `adjective-noun-NN` default as `project create`) makes a
ready-to-connect project with **no git and no model call**: the core's
reference target wrapped in a synthetic agent that echoes the first line of
its own skill file back at you, so an editor's change to that file shows up
in what the reference scorer sees.

It needs a configured service environment (`lab serve --execution-wheel
…`); without one it answers `409 service_environment_unconfigured`
([Local service reference](service-s5.md#service-environment-s7)) before the
project is created.

The demo bakes in, at creation time, the loopback port, the service
environment digest and the service interpreter path; after the service
environment changes (rebuilt wheels, a moved venv) or when the port gets
taken, create a new demo.

The project's display name is `Demo (<id>)`, so two demos are told apart in lists (W56). The
working copy is `<home>/clones/<id>/`, no git, one implementation
(`id: "demo"`, place `agent`):

| Entry | Content |
| --- | --- |
| `launch.json` | The target's launch file, `base_url` on a free loopback port, `environment_ref` set to the service environment's digest. |
| `agent/demo_agent.py` | The synthetic agent. |
| `lab/package.json` | The package entry of implementation `demo` (origin `connected`). |
| `lab/manifest.json` | The campaign manifest from the core's own scaffold, `target.base_url` and every role's `argv[0]` already filled in. |
| `lab/SPEC.md` | The task specification to confirm in `connect`'s `spec-confirm` step. |
| `lab/spec.answers.json` | The answers file of `connect`'s `spec` step, valid against `spec-answers/1`, next to `SPEC.md`. |
| `lab/evaluation-request.json` | A fixed-evaluation prepare body — with `"bundle_digest": null`. `lab project prepare` **refuses `null`** with `invalid_request`; use a copy outside the working copy with `bundle_digest` set to the Bundle's digest from `lab project bundle` before preparing — editing the file in place changes the materials revision and the Bundle digest, which stales the facts. |
| the scaffold's `corpus`, `scorer`, `editor`, `reflector`, `seed` | Written by `evolution.tree.reference_target_cli.scaffold`, unmodified. |

**The fault flag**, `<home>/clones/<id>.fault` — note the sibling path, not
inside the working copy: while it exists, every turn of the demo agent
answers `target_failed`, as the target's own fault. A campaign running
against it **suspends** rather than failing outright (the core's
`target_unstable`), visible as `result.core.suspensions[-1].reason` in `lab
status` — a free, deterministic way to exercise
[Resume](service-s5.md#resume). The flag sits outside every Bundle root, so
creating or removing it never changes a Bundle digest and never stales a
fact. To continue: stop the faulty target, remove the flag, start a fresh
target of the same Bundle, then `lab resume <operation> --target <id>
--generation <generation>`.

## Operation summary `lab-project-operation-list/2`

`GET /v1/projects/{id}/operations?view=summary[&implementation=I]` (CLI `lab project operations
ID --summary`) answers one row per link of the project, over all pairs:

```json
{"schema": "lab-project-operation-list/2", "project": "demo",
 "operations": [{"label": "Demo (demo) · evaluation", "link": {"…": "lab-project-link/1"},
                 "operation_state": "present", "operation": {"…": "lab-operation-summary/1"},
                 "outcome": null}]}
```

- `operation_state` is `present` (an operation of this pair: `operation` is its
  `lab-operation-summary/1`, see the [service reference](service-s5.md#progress-times-and-summaries)),
  `other_pair` (Lab never opens another pair's state, ADR-0017: `operation` is `null`, `outcome` comes
  from the link's last synchronised snapshot, and there is no progress, time or measurement) or
  `unavailable` (the operation could not be read).
- `label` is `<display name> · evolve · цикл N` for a campaign link, where N numbers the campaign
  links of the **whole project**, numbered 1…N in the order the links were stored, independent of the `implementation` filter, and
  `<display name> · <kind>` for the rest (for example `Demo (demo) · evaluation`). The operation
  summary itself carries no label or project (ADR-0017 item 4): the label exists only here.
- Without `view` the route still answers `lab-project-operation-list/1`.

## Routes

All routes are under `/v1`. Mutating routes take the S7 mutation-key headers
(`Lab-Request-Id`, `Lab-Owner-Generation`) and belong to their own class
`project` (limit 10,000 records per generation; the probe routes below form the class `probe`, limit 1,000); the key is checked before the body.
Bodies are closed and carry `schema`; `implementation: null` means "the only active
implementation" (for a confirmation: the project level).

| Method and path | Request | Response | Key route |
| --- | --- | --- | --- |
| `POST /projects` | `lab-service-project-create/1` `{schema, id, name, materials_root, implementations, source, exclude}` | 200 `lab-project/2` | `project_create` |
| `POST /projects/demo` | `lab-service-project-demo-request/1` `{schema, id}` | 200 `lab-project/2` | `project_demo` |
| `GET /projects[?archived=1]` | — | 200 `lab-project-list/2` `{schema, projects}` | — |
| `GET /projects/{id}` | — | 200 `lab-project/2` | — |
| `PATCH /projects/{id}` | `lab-service-project-update/1` `{schema, mutation, expected_record_version}` | 200 `lab-project/2` | `project_update` |
| `POST /projects/{id}/bundles` | `lab-service-project-bundle-request/1` `{schema, implementation}` | 200 `lab-project-bundle-result/1` `{schema, bundle: lab-bundle-summary/3, link}` | `project_bundle` |
| `POST /projects/{id}/prepared-executions` | `lab-service-project-prepare-request/3`: the fields of `lab-service-prepare-request/5` + `implementation`; `/2` (prepare `/3`, with `split`) and `/1` (prepare `/2`, no `split`) remain accepted | 200 `lab-project-prepared-result/1` `{schema, prepared: lab-prepared-summary/1, link}` | `project_prepare` |
| `POST /projects/{id}/targets` | `lab-service-project-target-request/1` `{schema, implementation, bundle_digest, secrets}` | 202 `lab-project-target-result/1` `{schema, task: lab-task-ref/1, link \| null}` | `project_target` |
| `POST /projects/{id}/ports` | `lab-service-project-port-request/1` `{schema, from, template, ref, id, into}` (see [Ports](#ports-lab-project-port)) | 200 `lab-project/2` | `project_port` |
| `POST /projects/{id}/sync/check` | `lab-service-project-sync-check-request/1` `{schema, rev}` (see [Sync](#sync-lab-project-sync)) | 200 `lab-project-sync-check/1` | `project_sync_check` |
| `POST /projects/{id}/sync` | `lab-service-project-sync-request/1` `{schema, upstream}` | 200 `lab-project-sync-result/1` | `project_sync` |
| `POST /projects/{id}/workspace` | — (empty body) | 200 `lab-project-workspace/1` `{schema, path}` | `project_workspace` |
| `POST /projects/{id}/operations` | `lab-service-project-start-request/1`: the fields of `lab-service-start-request/1` + `implementation` | 202 `lab-project-start-result/5` `{schema, operation: lab-operation-status/8, link \| null}` | `project_start` |
| `GET /projects/{id}/operations[?implementation=I]` | — | 200 `lab-project-operation-list/1` `{schema, links}` (all pairs) | — |
| `GET /projects/{id}/operations?view=summary[&implementation=I]` | — | 200 `lab-project-operation-list/2` (see [Operation summary](#operation-summary-lab-project-operation-list2)) | — |
| `GET /projects/{id}/facts[?implementation=I]` | — | 200 `lab-project-facts/5` | — |
| `GET /projects/{id}/status[?implementation=I&scenario=S]` | — | 200 `lab-project-status/6` | — |
| `GET /scenarios` | — | 200 `lab-scenario-list/1` | — |
| `GET /scenarios/{id}` | — | 200 `lab-scenario-entry/1` | — |
| `GET /scenarios/{id}/schemas/{name}/{n}` | — | 200 the schema file as shipped (`application/schema+json`) | — |
| `POST /projects/{id}/confirmations` | `lab-service-confirm-request/1` | 200 `lab-project-confirmation/1` | `project_confirm` |
| `GET /projects/{id}/confirmations[?revoked=1]` | — | 200 `lab-project-confirmation-list/1` `{schema, confirmations}` | — |
| `POST /projects/{id}/confirmations/{cid}/revoke` | `lab-service-revoke-request/1` `{schema, reason}` | 200 `lab-project-confirmation/1` | `project_revoke` |
| `POST /projects/{id}/acceptances` | `lab-service-accept-request/2` (required `expected`: `null` or non-empty `[{id, from, to}]`); `/1` remains supported without `expected` | 200 `lab-project-acceptance/1` | `project_accept` |
| `POST /projects/{id}/acceptances/{aid}/revoke` | `lab-service-revoke-request/1` | 200 `lab-project-acceptance/1` | `project_revoke` |
| `POST /projects/{id}/probes` | `lab-service-project-probe-request/1` (see [Probe sessions](#probe-sessions)) | 201 `lab-probe-session/1` | `project_probe_start` |
| `GET /projects/{id}/probes[?implementation=I]` | — | 200 `lab-probe-session-list/1` | — |
| `GET /probes/{session}` | — | 200 `lab-probe-session/1` (with `turns`) | — |
| `POST /probes/{session}/turns` | `lab-service-probe-turn-request/1` `{schema, content}` | 200 `lab-probe-turn/1` | `probe_turn` |
| `POST /probes/{session}/reset` | — (empty body) | 201 `lab-probe-session/1` (the new session) | `probe_reset` |
| `DELETE /probes/{session}` | — | 200 `lab-probe-session/1` | `probe_close` |
| `GET /operations?project=P[&implementation=I]` | — | 200 `lab-operation-list/8` (current pair; `view=summary`: `lab-operation-summary-list/1`) | — |

- The project target route binds the Bundle to a connection (idempotent within the
  generation) and starts a target with the implementation's `client_ref`. Check and
  conformance then run through the **general** routes on that target
  (`lab connect BUNDLE` gives the connection id); their outcomes land in the
  target's link. Note that conformance stops its target when it finishes.
- A Bundle, PreparedExecution or start must belong to the same project and
  implementation (its `bundle` or `prepared` link, in this pair and state root);
  otherwise `not_linked`.
- `POST /projects/{id}/ports` is keyed like every project mutation (class `project`); the
  key is checked before the body. It can take long (a template fetch): the fetch runs off
  the service's IO worker, so other project calls are not queued behind it.
- `POST /projects/{id}/sync/check` and `/sync` are keyed like every project mutation (class `project`),
  although the check changes only `refs/remotes/origin/*` and tags; a repeated key returns the stored
  answer without a second fetch. The check can take long (a fetch), so it does not hold the session's
  mutation lock.
- A keyed dry-run acceptance is an ordinary keyed request: its preview is the
  recorded answer for that key.

## Errors

Project failures use the service's usual body with stage `project`:

```json
{"error": {"code": "home_busy", "stage": "project", "message": "Another Lab process writes this Lab home, or this handle cannot write it."}}
```

| Code | HTTP | When |
| --- | --- | --- |
| `invalid_request` | 400 | A malformed body, mutation, path, claim or reason. |
| `invalid_id` | 400 | An id outside `^[a-z][a-z0-9-]{1,39}$`. |
| `ambiguous_implementation` | 400 | `implementation: null` on a project with several active implementations. |
| `reserved_claim` | 400 | A confirmation claims a computed fact's name. |
| `project_not_found` | 404 | No such project (also, currently, an unknown confirmation or acceptance id). |
| `implementation_not_found` | 404 | No such implementation, or none is active. |
| `not_found` | 404 | `GET /scenarios/{id}` with an id that is not in the catalogue; `GET /scenarios/{id}/schemas/{name}/{n}` for a scenario or schema that is not declared. |
| `id_taken` | 409 | The id is used by a project that is not `failed` (archived ones included); for `port`, the port id is already an implementation of the project (archived ones included). |
| `archived` | 409 | Work on an archived project or implementation. |
| `implementation_archived` | 409 | `port --from` names an archived implementation. |
| `port_destination_exists` | 409 | The port destination exists and is not an empty directory, including a directory a crashed port left. The message ends with the destination path **only** for the default destination (`LAB_HOME/ports/<project>/<port-id>`); with `--into` it carries no path (the service never echoes request data). |
| `active_operation` | 409 | The mutation touches an active operation. |
| `conflict` | 409 | `expected_record_version` differs from the current one. |
| `project_not_ready` | 409 | The project is `creating` or `failed`. |
| `not_linked` | 409 | The Bundle, PreparedExecution or target is not linked to this project and implementation in this pair. |
| `inputs_changed` | 409 | The revision or consumed inputs changed during preparation or acceptance inventory collection; repeat `dry_run` for acceptance. |
| `digest_mismatch` | 409 | The confirmed bytes differ from the current bytes, or the acceptance state differs from `expected`; repeat `dry_run` for acceptance. |
| `not_acceptable` | 409 | Nothing to accept, or `only` names a fact that cannot be accepted. |
| `home_busy` | 409 | Another process writes this Lab home, or the registry was opened read-only (Python API). |
| `registry_newer` | 409 | The registry was written by a newer, incompatible Lab. |
| `port_into_working_copy` | 400 | `--into` is the project's working copy or inside it. |
| `port_into_lab_home` | 400 | `--into` is the Lab home or anywhere inside it. |
| `git_unavailable` | 422 | `git` is not on the service's `PATH` (checked before any write). |
| `template_not_git` | 422 | The template is not a git repository: a local path that is not the top level of a repository or a bare repository, or a URL that git does not recognise as one. |
| `template_ref_not_found` | 422 | The template has no such revision. |
| `template_fetch_failed` | 422 | git could not read the template (unreachable, refused, timed out). The body is closed; git's stderr tail goes to the service log with credentials redacted. |
| `not_a_clone` | 422 | `sync` on a project made with `--path`, without `source`, or whose working copy is no longer `clones/<id>/` (after `set --working-copy`). |
| `sync_fetch_failed` | 422 | `git fetch` could not read the source (unreachable, not git, ssh refused). The body is closed; git's stderr tail goes to the service log with credentials redacted. |
| `revision_not_found` | 422 | `sync --rev` did not resolve; or the source has no `origin/HEAD` and no `--rev` was given. |
| `history_diverged` | 409 | `source.base_revision` is not an ancestor of `upstream` (the source rewrote history). |
| `branch_mismatch` | 409 | `sync --apply`: the working copy is not on `lab/<id>`. |
| `working_copy_dirty` | 409 | `sync --apply`: tracked files are modified, or a merge or rebase is unfinished. The paths are in the message. |
| `merge_conflict` | 409 | `sync --apply`: the merge conflicts, or git (or Lab, for ignored files) would overwrite an untracked or ignored local file. The copy is as it was; up to 50 paths are in the message. |
| `merge_failed` | 409 | `sync --apply`: the merge failed without a conflict (hook, signing, other); the copy is restored, or the message says a person must repair it. git's stderr tail goes to the service log. |
| `upstream_changed` | 409 | `sync --apply`: the commit is not in the clone (no check was run, or the objects are gone), or it is not reachable from the source or a commit shown by a check. |
| `sync_not_recorded` | 500 | `sync --apply`: merged, but `base_revision` was not recorded; repeat with the same sha and a new mutation key. |
| `port_failed` | 500 | Creating the port repository failed (`git init`, commit or tag). Nothing is left behind. |
| `tree_not_found` | 404 | A folder or file being linked does not exist, is a symlink, or (for a folder) has no inventoried entries. |
| `tree_excluded` | 422 | A linked/read folder overlaps a project exclusion or the nested materials-root cut; detail identifies the pattern or cut. |
| `working_copy_unavailable` | 422 | `--path`/`set_working_copy` or a tree implementation place is not an accessible directory. |
| `package_unavailable` | 422 | The package entry is set but missing, unreadable or carries `schema` (an implementation with no package entry falls back to the entry file, else `target_undeclared`). |
| `package_mismatch` | 422 | No Bundle root covers the implementation's place. |
| `clone_failed` | 422 | `git clone`/`checkout` failed. The body is closed; git's stderr tail goes to the service log only ([Creating a project](#creating-a-project)). |
| `target_undeclared` | 422 | The implementation has neither a package entry nor a `lab-target-entry.json` in its place (`target_unavailable` is an Owner code, not this one). |
| `target_entry_invalid` | 422 | `lab-target-entry.json` is malformed; the `detail` names the field or says "unknown key". A file that is not a JSON object or has another `schema` is `unsupported_schema`. |
| `bundle_probe_only` | 409 | A campaign, evaluation or conformance on a Bundle of kind `probe`; it only raises a target for probe sessions (stage `before_start`). |
| `probe_sessions_open` | 409 | `check`, `conformance` or an operation on a target that has probe sessions not yet released: close or reset them, or stop the target (stage `before_start`). |
| `target_slot_held` | 409 | `lab project target` while another target of this Owner is `ready` with a different Bundle, launch or project; stop it first (`lab agent list --ready`, `lab agent stop`). The same Bundle with other secrets stays `binding_mismatch` (stage `before_start`). |
| `probe_session_not_found` | 404 | No session with this id. |
| `probe_session_closed` | 409 | A turn or reset on a session that is `closed`. |
| `probe_ceiling_reached` | 409 | A set ceiling (turns, tokens or cost) of the session chain is spent; checked before the turn. |
| `probe_ceiling_unverifiable` | 409 | A tokens or cost ceiling is set, but the previous turn's usage or the gateway cost could not be read. |
| `probe_limit_reached` | 409 | No free instance: the target's `max_instances` are held by probe sessions. |
| `target_not_ready` | 409 | The implementation has no live target of this service in state `ready`. |
| `cleanup_required` | 409 | An unsettled probe release; only a target stop helps. |
| `link_failed` | 500 | The action ran, but its link was not recorded; repeat with a new request id. |
| `home_invalid` | 500 | The home path or directory fails the checks above. |
| `registry_invalid` | 500 | The registry file is unreadable or from an older major version. |

Errors of the underlying Bundle, prepare and Owner calls (for example
`unsupported_input`, `target_not_running`) keep their own stage and code.

## `lab project` commands

The commands talk to the service like the other client commands: the globals
(`--socket`, `--json`, `--timeout`, `--request-id`/`--generation`,
`--no-request-id`) come first, and every mutating command sends a fresh mutation
key unless you give one. One example per group (`S=/home/you/lab-run/lab.sock`):

```bash
# Register and list
lab --socket $S --json project create acq --name Acquiring --path /home/you/acquiring --root lab \
  --impl agent=lab/agent,package=bundle-request.json
lab --socket $S --json project create --name Payments --clone git@example:team/payments.git --rev main --root lab
lab --socket $S --json project list --archived
lab --socket $S --json project show acq

# Change the project and its implementations
lab --socket $S --json project rename acq "Acquiring v2"
lab --socket $S --json project set acq --exclude "data/tmp/**" "node_modules/**"
lab --socket $S --json project impl add acq port=lab/port,package=port-request.json,origin=ported_from:agent
lab --socket $S --json project impl set-package acq port --none
lab --socket $S --json project archive acq --reason "moved to the team repository"

# Bring a Lab clone up to date with its source: check, then apply the commit the check showed
lab --socket $S --json project sync acq
lab --socket $S --json project sync acq --apply 3f9c0a1d5b7e2c4a6d8f0b1c3e5a7d9f1b2c4e6a

# Port an implementation onto a template (a URL or an absolute path; @REF is optional)
lab --socket $S --json project port acq --from agent --template https://git.example.com/team/sylph.git@v1
lab --socket $S --json project port acq --from agent --template /srv/templates/sylph --id agent-sylph --into /home/you/ports/sylph

# Prepare and run through the project (the general check/conformance run on its target)
lab --socket $S --json project bundle acq
lab --socket $S --json project target acq --bundle BUNDLE_DIGEST
lab --socket $S --json project prepare acq --request /home/you/evaluation-prepare.json
lab --socket $S --json project start acq --prepared PREPARED_DIGEST --target TARGET

# Read history and facts
lab --socket $S --json project operations acq --implementation agent
lab --socket $S --json project facts acq

# Confirm and accept
lab --socket $S --json project confirm acq --claim spec.confirmed --path SPEC.md \
  --link catalogue:requirements --author vitaly
lab --socket $S --json project confirmations acq --revoked
lab --socket $S --json project accept acq --reason "renamed the prompt file" --yes
lab --socket $S --json project revoke-acceptance acq a-f1bc72b28b3a1eb6 --reason "wrong baseline"
```

- `--impl ID=PLACE[,package=REL][,name=NAME][,origin=connected|created|ported_from:ID]`
  is repeatable on `create`; `project impl add` takes one such spec.
- `project sync ID [--rev R | --apply SHA]` is [sync](#sync-lab-project-sync): with no flag or with `--rev`
  it checks, with `--apply` it merges that exact commit (the two flags exclude each other).
- `project port ID --from SOURCE --template SRC[@REF] [--id PORT_ID] [--into DIR]` is the
  [port](#ports-lab-project-port) command; `--into` is made absolute from the current
  directory, like the other local paths of `lab project`.
- `project prepare --request FILE` reads a prepare body without `schema`, like `lab prepare`.
  Evaluation settings select project request `/3`, with omitted nullable settings filled with `null`;
  older files select `/2` when they name `split`, otherwise `/1`. A fixed evaluation prepared from a campaign
  Bundle resolves omitted failure settings to Lab's `retry` defaults
  ([why](service-s5.md#fixed-evaluation-budget)).
- `project start` takes the target's generation from the current `health`; a target
  of an earlier generation cannot be used.
- `--implementation I` is needed only when the project has several active
  implementations.
