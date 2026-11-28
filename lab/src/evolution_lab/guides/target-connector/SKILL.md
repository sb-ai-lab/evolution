---
name: evolution-lab-target-connector
description: Connect an agent to Lab as a target through four seams, the neutral turn contract and the lab-target-launch/2 or /3 file.
---

# Target connector

For a first baseline from a project directory, start with [first evaluation](../first-evaluation/SKILL.md) and return here for the adapter contract.

Lab measures an agent as a *target*: a process that serves the core
`/evolution/v2` wire at a declared address and answers one turn at a time. This
guide describes what an agent must offer so that such a target can be built
(four seams), the neutral turn contract and launcher Lab ships to build one,
capacity, and the offline conformance mode.

Status: the neutral turn contract and the Lab launcher (`lab-target-launch/2` and `/3`)
ship with Lab; an agent implements `build` and reaches a target through them
without touching the core wire directly. Binding a specific agent framework to
the neutral contract is the framework's own job, not Lab's — see
[Template bindings](#template-bindings). Catalogue item ids do not change when
this guide is extended.

Examples use a synthetic *order-lookup assistant* with one `lookup_order` tool.
It stands for any agent; nothing in Lab depends on its shape.

## Setup order: environment, then connector

1. Prepare the **target environment first**, outside the implementation's
   place. Install the agent's dependencies and the producing `evolution-lab`
   version with its exact core pin there. A checkout on `PYTHONPATH` is not an
   installed target environment: Lab starts its interpreter with `-I`.
2. Then write the connector's synchronous `build(context)` and its
   `TurnAgent`. Declare every setting in `env` or `secret_names`; do not
   depend on the shell's ambient environment. Keep connector source in the
   implementation, not the environment itself.
3. Declare the entry below (or a package's [launch file](#launch-file)), then
   use the [import-and-build recipe](#entry-self-check) only when explicitly
   requested. It is not a protocol self-test or a probe.

Recommended layout: install the agent as wheel(s) in the target environment
and keep the adapter source in the Bundle. Retain and declare the agent wheels
alongside the Lab/core pair and any dependency wheels whose bytes matter to
the run. Lab copies the adapter's declared `program_paths`; environments and
wheel archives stay outside the copied roots and remain external prerequisites.

For a package launch, `identity.runtime_ref_digest` is still declared manually
by the integration. Lab does not derive it automatically from the target
environment or its wheels. Keep it consistent with the runtime you intend to
identify; an environment descriptor does not fill this identity field.

<a id="target-entry"></a>
## No package yet: `lab-target-entry.json`

An implementation without a package can still start as a live target. Put
`lab-target-entry.json` in the root of its place, under git:

```json
{
  "schema": "lab-target-entry/1",
  "agent": {"module": "port_agent.lab", "attribute": "build", "import_root": "src"},
  "program_paths": ["src"],
  "environment": {"executable": "/home/me/.venvs/port-a/bin/python", "wheel_paths": ["/home/me/wheels/port_agent-1.0.0-py3-none-any.whl"], "runtime_files": []},
  "env": {"TARGET_MODEL": "example-model"},
  "secret_names": ["TARGET_API_KEY"],
  "limits": {"max_instances": 2, "max_concurrent_sessions": 1, "max_input_bytes": 1048576, "max_output_bytes": 65536},
  "startup_timeout_seconds": 30,
  "stop_grace_seconds": 5
}
```

The example names one wheel archive; use the actual installed agent's
wheel path and extend the list for the environment you declare. Preparation
requires at least one wheel archive: `wheel_paths: []` is refused with
`environment_unavailable`, even though the entry reader accepts an empty list.

### Field reference: `lab-target-entry/1`

The reference follows the installed validator module
`evolution_lab.projects.entry` (`src/evolution_lab/projects/entry.py` in the
source distribution), specifically `read_target_entry`. There is no separate
packaged JSON Schema. The top-level key set is closed; unknown keys are refused.

| Field | Required | Allowed values / default |
|---|---|---|
| `schema` | yes | Exactly `"lab-target-entry/1"`. |
| `agent` | yes | Exactly `{module, attribute, import_root}`, all nonempty strings. `module` names the importable dotted Python module; `attribute` names its synchronous builder (usually `build`). Preparation also checks module/attribute syntax. |
| `agent.import_root` | within `agent` | A nonempty relative POSIX path inside the place, without `..`, backslashes or NUL; `"."` means the place root. **Not `null`** in entry `/1`. |
| `program_paths` | yes | A nonempty list of relative POSIX paths with the same path restrictions as `import_root`; declare the connector materials to copy into the Bundle. |
| `environment` | yes | Either exactly `{"kind": "service"}` (use the service's environment), or exactly `{executable, wheel_paths, runtime_files}`: `executable` is a string, the other two are lists of strings (possibly empty). All paths must be absolute, without NUL, and resolve **outside** the place. |
| `env` | yes | An object of string names to string non-secret values; may be empty. |
| `secret_names` | yes | A list of distinct nonempty strings, disjoint from `env` names; may be empty. Names only, never secret values. |
| `limits` | no | An object containing any subset of `max_instances`, `max_concurrent_sessions`, `max_input_bytes`, `max_output_bytes`; no other keys. Each value is a positive integer, not a boolean. Missing values default to `2`, `1`, `1048576`, `65536`, respectively. |
| `startup_timeout_seconds` | no | Positive number, not a boolean; default `30`. |
| `stop_grace_seconds` | no | Positive number, not a boolean; default `5`. |

These are the entry reader's field rules, not proof that preparation or launch
will succeed. Declare only legal process-environment names, and do not use the
reserved `LAB_TARGET_CONFIG_FD` / `LAB_TARGET_READY_FD` names. Supply secrets
privately at target start. Keep the target environment outside the place (for
example with `UV_PROJECT_ENVIRONMENT`); a path inside it is refused, naming
`environment`.

Unlike entry `/1`, a package's launch `/2` allows `agent.import_root: null`
when the connector is installed in the target environment. Otherwise launch
uses a Bundle-relocated directory, put first on `sys.path`. Entry `/1` always
declares a directory in the place; Lab relocates it when synthesizing launch
`/2` and a policy point in a Bundle of kind `probe`. You do not write that
launch file yourself.

Which source a target takes (`lab project target`): the package wins when the
implementation has one (`package_entry`); otherwise the entry file; with
neither, `target_undeclared` (422). A bad file is `target_entry_invalid`
(422; the `detail` names the field, or says "unknown key"); a file that is not
a JSON object or has another `schema` is `unsupported_schema`.

An entry target has **no protocol self-test**: its synthesized launch has
`conformance: null`. A Bundle of kind `probe` only raises a target for probe
sessions. Preparation refuses prepared execution of it (`artifacts_invalid`);
a campaign or fixed evaluation start, a resume or conformance on its target
is refused with `bundle_probe_only` (409) before anything runs. Use a probe
for candidate-dependent checks, not conformance. Editing the file changes the
place's digest like any code change, so confirmations go stale honestly. Item:
{req:target.entry_file}.

## The four seams

A seam is a property of the agent's code, not a Lab file. Each seam below says
which part of the current path it serves and what is lost without it.

<a id="explicit-settings"></a>
### 1. Explicit settings

Every value that changes the agent's behaviour reaches it through the launch
file's `env` and `secret_names`. The built-in `/2`–`/3` launcher also receives
Lab's effective `EVOLUTION_DURABILITY`, replacing a carried value, and inherits
Lab's `TMPDIR` when no target value is declared. These host settings do not
change Bundle/prepared identity. The target inherits no other variables, and the launcher builds
`LaunchContext.env`/`LaunchContext.secrets` from that same process
environment, so the agent may read them either from `context` or from
`os.environ` (ADR-0016); no code change is needed for an agent that already
reads its environment. What breaks the seam is reading a behaviour-changing value from
outside that set — a file in the home directory, a config service. For the
example: the model name, the API key and the order-store address are declared
in the launch file and read in `build(context)`.

- Serves: the launch file's `env` and `secret_names`, plus the two host
  settings described above — the Lab launcher splits
  them into `LaunchContext.env`/`LaunchContext.secrets` and passes that to
  `build` (see [Neutral turn contract](#neutral-contract); the process also
  receives a runtime configuration, `lab-target-runtime/1` or its `/2`
  extension matching the launch schema, on start — see
  [Lifecycle and health](#launch-file); `owner/target.py:46-60`), and the
  `settings_hash` in its identity.
- Without it: behaviour depends on whatever environment happens to be present,
  runs are not reproducible, and a change of a hidden value is invisible to the
  campaign identity. Lab does not merge ambient environment variables into the
  target process.
- Items: {req:agent.models}, {req:agent.config}; it also carries
  {req:world.reference_time} and {req:agent.skills_layout} when the agent reads
  them from settings.

<a id="isolated-session"></a>
### 2. Isolated session

There is a documented way to get a clean conversation state, and no mutable
state is shared between sessions. Where the state lives is the agent's
choice: it may keep it itself under the session id and drop it in
`close_session`, or return it as JSON from `open`/`turn` so the neutral layer
keeps it. For the example: `new_session()` returns an empty history object
that becomes the session's initial state; the agent's own bookkeeping stores
it per session and drops it on close.

- Serves: the neutral contract's `TurnAgent.open(public, *, session_id, seed)`
  and `close_session(session_id)` (see
  [Neutral turn contract](#neutral-contract)), and the session limits in the
  launch file.
- Without it: one case leaks into the next, repeats of the same case are not
  independent, and concurrent sessions interfere.
- Items: {req:agent.session_state}, {req:agent.entrypoint},
  {req:agent.request_contract}.

<a id="turn-trace"></a>
### 3. Turn trace as data

A turn exposes its tool calls and results, loaded skills, usage, the final
answer and the turn outcome as a structure, not only as text or logs.

- Serves: the neutral contract's `AgentTurn` (`events`, `usage`, `outcome` of
  `ok`, `candidate_failed` or `target_failed`, and an optional `reason`; see
  [Neutral turn contract](#neutral-contract) and
  [Turn outcomes](#turn-outcomes)).
  `latency_ms` is measured by the server, not the agent. The scorer later
  receives the recorded history in its `view`.
- Without it: the scorer can judge only the final text; tool misuse, skipped
  confirmations and cost stay invisible to measurement and to the reflector.
- Items: {req:agent.tools}.

<a id="injected-backends"></a>
### 4. Injected backends

Tools reach external systems through interfaces whose implementation is passed
in when the agent is built. The measured world is then replaced without editing
the agent. For the example: `lookup_order` calls an `OrderStore` interface; the
target passes an in-memory store filled from the case's `public` data (or a
fixture it names) when the session opens.

- Serves: the backend `load`/`open` calls, which build the agent with a
  simulated world, and the case data that fills it. The session is opened
  with the case's `public` only; `private` stays with the environment and never
  reaches the agent.
- Without it: a run touches real systems, results depend on their current
  state, and actions with consequences cannot be checked safely.
- Items: {req:world.systems}, {req:agent.tools}, {req:agent.effects_guard}.

<a id="neutral-contract"></a>
## Neutral turn contract

`evolution_lab.target` is Lab's own public, versioned contract: a template's
binding to an agent framework depends on it the same way Lab depends on core.
It has no import of any agent framework of its own.

```text
AgentFactory = Callable[[AgentView], Awaitable[TurnAgent]]

AgentView(root, candidate_digest, dependency_digest, policy, seed_mode)  # read-only

class TurnAgent(Protocol):
    def describe(self) -> Mapping[str, Scalar]: ...  # flat; ≤32 keys, ≤4096 B
    async def open(self, public, *, session_id, seed) -> JsonValue | None: ...
    async def turn(self, message, state: JsonValue | None, *, session_id, seed) -> AgentTurn: ...
    async def close_session(self, session_id) -> None: ...
    async def aclose(self) -> None: ...

AgentTurn(outcome, text, events=(), state=KEEP, usage=None, truncated=False,
          reason=None, provenance=None)  # outcome: ok | candidate_failed | target_failed
                        # state: JsonValue | None | KEEP
TurnReason(category, code)  # both [A-Za-z0-9._-]{1,64}
LaunchContext(env: Mapping[str, str], secrets: Mapping[str, str],
              work_directory: Path)  # immutable env/secrets; private process cwd

Scalar = str | int | float | bool | None  # float finite
JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]  # float finite

class TargetConfigurationError(Exception): ...
```

An agent implements `build(context) -> AgentFactory`, named by the launch
file's `agent.module`/`agent.attribute` (see [Launch file](#launch-file)).
`context` is a `LaunchContext(env, secrets, work_directory)`: the launch
file's non-secret `env`, the secret values named in `secret_names`, and a
private directory for the process. These mappings are built from the
variables Lab declared for this process (seam 1). `build(context)`
runs once per process; the layer then awaits `factory(view) -> TurnAgent` once
per instance, and drives `open`/`turn`/`close_session`/`aclose` as described in
[One turn](#turn-entrypoint).

- **One event loop per instance.** `build(context)` runs once, synchronously,
  on the launcher's main thread, before any instance loop exists — there is
  no running loop under it. `factory(view)` and every `open`/`turn`/
  `close_session`/`aclose` of one instance run on that instance's own thread
  and event loop. Loop-bound resources (an async client, anything tied to a
  running loop) belong in `factory(view)`, not in `build`: each instance has
  its own loop, so a resource built in `build` would be bound to none of
  them.
- **Turns are sequential per instance.** Core's instance lock, not the layer,
  serializes the turns of one instance (exact core 2.0.0b22); two sessions on
  the same instance never run turns at the same time. Campaigns can use
  `limits.max_instances` in the launch file (see [Capacity](#capacity)). Fixed
  evaluation can schedule several samples on one instance with
  `concurrency <= limits.max_concurrent_sessions`; it does not remove Core's
  serialization of turns. Work outside turns and independent scoring can overlap,
  but several sessions alone do not guarantee a several-times baseline/holdout
  speedup when agent turns dominate.
- **`state=KEEP`.** `turn` returns the next session state, or the `KEEP`
  sentinel to leave the previous state unchanged — the shape used by an agent
  that drops its own state on a failed turn.
- **State is JSON.** The state `open` returns and every non-`KEEP`
  `AgentTurn.state` must be a `JsonValue` with finite numbers; anything else
  (a client object, an open file, `NaN`) fails that call with a
  `BackendError`, and a refused turn leaves the previous state in place. The
  layer keeps its own JSON copy, so changing the returned object afterwards
  does not change the state the next turn receives. The state advances only
  once the whole reply conforms to core: a turn core rejects leaves the session
  where it was.
- **Fitting to `max_output_bytes`.** The layer trims a turn's `events` and,
  last, its `text` to fit the launch file's `limits.max_output_bytes` before
  it goes back to core; the final `assistant_message` built from
  `AgentTurn.text` is the one thing never dropped, even when everything else
  has to be trimmed away.
- **`describe()` carries no secrets.** It is a one-time statement about the
  built agent (configuration digest, where settings came from, where the seed
  goes), not an observation of turns: a flat mapping of scalars, at most 32
  keys and 4096 bytes of canonical JSON (see
  [Instance description](#turn-outcomes)). A literal value of any launch
  secret inside it refuses the instance load. Also follow the stricter
  no-role/scorer-environment-values rule there; use a settings digest, not
  raw model names or URLs.
- **`TargetConfigurationError`.** Raise it only when the process
  configuration itself is broken and every sample on it would fail — a
  missing required setting, an unreachable model endpoint declared in `env`.
  A single session's or a single turn's failure is not a configuration error:
  return an `AgentTurn` instead — `outcome="candidate_failed"` for a
  deterministic defect of the candidate, `outcome="target_failed"` for an
  agent-internal timeout or a failure the agent cannot tell apart (see
  [Turn outcomes](#turn-outcomes)).

**`exact` seed for an LLM agent.** `identity.seed_mode` states how repeats are
paired; the layer's only job is delivery — under `exact` it passes the
session's requested seed unchanged into every `open` and `turn` call and
requires `effective_seed` to equal it. What the agent does with that seed is
its own contract, declared in `describe()`. For an LLM-backed agent, passing a
seed through does not make a repeat's output identical: providers are not
deterministic at any temperature, and a chain of tool calls or an injected
backend can still diverge between repeats. Declare `exact` only when the seed
actually reaches something that narrows this (a fixed sampling seed on the
model call, a deterministic substitution for injected backends); otherwise
declare `unpaired` or `none`.

### Report what the agent used

This section describes development source; the latest released dev72 does not include these SDK additions.

For `gate.provenance.kind: reported`, Core uses the baseline's reported inputs to select affected cases for each attempt. Return what the agent actually used on **each turn**; Lab does not infer this from `skill_read` or other events. Paths are relative to the loaded tree root (`AgentView.root`):

```python
from evolution_lab.target import AgentTurn, TurnProvenance

return AgentTurn("ok", answer, provenance=TurnProvenance(used=("skills/SKILL.md",)))
```

`provenance` is optional on any outcome. If the mandatory reply and provenance exceed `max_output_bytes`, Lab drops the whole provenance, never part of `used`; unknown inputs are treated conservatively as affected. Otherwise events are fitted into the remaining budget. Avoid secrets in `used`.

When a shared prompt includes an index of many skills, path-level reporting may mark all of them affected. Optionally provide `components()` on the agent: a map from component identifiers to rendered-content digests and source paths. Report the identifiers actually used in `TurnProvenance.used`. For example, for an index rendered from two files and one skill body:

```python
from evolution_lab.target import component

def components(self):
    return {
        "skill-index": component(self.rendered_index, ["skills/alpha.md", "skills/beta.md"]),
        "skill-alpha": component(self.alpha_body, ["skills/alpha.md"]),
    }
```

`component` hashes UTF-8 text or bytes and returns `sha256` and `sources`. Lab validates the map and scans it and `used` for launch secrets. An absent `components()` differs from an empty map. Core measures the complete instance-creation response against `max_output_bytes`: Lab does not reject, trim or drop a map for its size. Lab logs a warning when its upper bound exceeds the limit; this can be a false positive. Core can reject the actual oversized response with HTTP 500. Inspect target logs and increase the manifest limit or reduce the map if needed.

A campaign notice `baseline_provenance_unknown` means Core stored no provenance for any sample in the latest completed baseline: the target may have omitted it, including on some turns. With `reported`, every attempt then treats all cases as affected. Check that every turn reports its actual inputs and that the response limit is sufficient, then run a new campaign baseline. The notice is informational; partial baseline provenance does not produce it. Lab built with Core's evaluation provenance support shows the same warning in `lab evaluate` for a `reported` project when finished samples carry no provenance. A pre-marker evaluation is identified by `provenance_not_recorded`.

<a id="entry-self-check"></a>
## Import-and-build self-check recipe

This is guide text, not a Lab command or an automatic check. Run it only on
explicit request: importing a connector and calling `build` executes its code
and is not guaranteed to be free of side effects or model spend.

For a reviewed entry file, the driver below starts
`<environment.executable> -I -c ...` with **only** the declared `env` and
supplied secrets, and with a private `work_directory` as cwd, just like the
launcher. This matters even when the connector reads `os.environ`, not
`context`. Prepare an empty private scratch directory outside the place;
do not reuse a running target's directory. Entry `/1` has no
`work_directory` field: Owner creates it at launch; this scratch directory
stands in for that runtime value.

Replace the three absolute paths. The secrets file is a private JSON object
mapping exactly the declared secret names to their string values (use `{}`
when there are none). Obtain it through your normal private secret channel;
never put secret values in this code, shell history or argv. Keep both the
file and scratch directory accessible only to their owner. For
`environment: {"kind": "service"}`, replace the executable assignment with
the service's declared environment executable, not the driver's Python.

```python
import json
import subprocess
from pathlib import Path

entry_path = Path("/absolute/implementation/lab-target-entry.json")
work_directory = Path("/absolute/private/entry-check-work")  # existing private directory
secrets_path = Path("/absolute/private/target-secrets.json")
entry = json.loads(entry_path.read_text(encoding="utf-8"))
supplied = json.loads(secrets_path.read_text(encoding="utf-8"))
if (not isinstance(supplied, dict) or set(supplied) != set(entry["secret_names"])
        or not all(isinstance(value, str) for value in supplied.values())):
    raise ValueError("supply exactly the declared secrets as strings")
executable = entry["environment"]["executable"]
agent = dict(entry["agent"])
if agent["import_root"] is not None:
    agent["import_root"] = str((entry_path.parent / agent["import_root"]).resolve())
config = {"agent": agent, "secret_names": entry["secret_names"]}
code = r'''
import importlib
import json
import os
import sys
from pathlib import Path
from evolution_lab.target import LaunchContext

config = json.load(sys.stdin)
names = set(config["secret_names"])
context = LaunchContext(
    env={k: v for k, v in os.environ.items() if k not in names},
    secrets={k: v for k, v in os.environ.items() if k in names},
    work_directory=Path.cwd(),
)
agent = config["agent"]
if agent["import_root"] is not None:
    sys.path.insert(0, agent["import_root"])
build = getattr(importlib.import_module(agent["module"]), agent["attribute"])
factory = build(context)  # synchronous, no running event loop
if not callable(factory):
    raise TypeError("build(context) must return a callable AgentFactory")
print("import and synchronous build returned a callable; candidate not checked")
'''
subprocess.run(
    [executable, "-I", "-c", code],
    input=json.dumps(config), text=True, check=True,
    env={**entry["env"], **supplied}, cwd=work_directory,
)
```

The `import_root is not None` branch mirrors launch `/2`: `null` imports an
installed connector without changing `sys.path`; an entry file instead
requires a relative directory, resolved here inside the place. Under `-I`,
cwd and `PYTHONPATH` do not supply missing imports. Do not import
`evolution_lab.target.launch` for this check: that executable module consumes
Owner's inherited protocol descriptors at import time.

**Proof boundary:** this checks the import and synchronous `build(context)`
with the supplied launch settings, and only that its result is callable.
`AgentFactory` is a `Callable[[AgentView], Awaitable[TurnAgent]]` type alias,
not a runtime class: `isinstance(factory, AgentFactory)` is not a valid
check. The recipe does **not** call or await `factory(view)`, create a
`TurnAgent`, or call `describe()`: those need an actual candidate snapshot
and are checked when a probe opens an instance. It proves neither the full
factory/turn contract nor wheel identity, wire conformance, quality, cost or
cleanup. It also uses the current connector files, not a prepared Bundle's
relocated copy. No target server or probe is started by this recipe.

## Current path: core target server

A target is a process that runs core `evolution.tree.target.target_server`:
`TargetServer` owns the wire, instance identity, idempotency and the artifact
snapshot; it calls a `TargetBackend`, which never serves the wire itself.
`evolution_lab.target.backend(factory, *, secrets)` builds that `TargetBackend`
from an `AgentFactory` (see [Neutral turn contract](#neutral-contract)) — an
agent implements the neutral contract, not `TargetBackend` directly.

```text
TargetServer(backend(factory, secrets=...), **identity, **limits)
```

`AgentView.root` names a server-owned read-only snapshot of the candidate tree
and the facts attested about it (`candidate_digest`, `dependency_digest`,
`policy`). Build the agent from files under that root, never from the live
source tree. Raising from the agent is a fault: it stops the campaign at
once. A single failed turn never raises — it returns an `AgentTurn` with
`outcome="candidate_failed"` (a deterministic defect) or
`outcome="target_failed"` (an agent-internal timeout, or when it cannot tell;
see [Turn outcomes](#turn-outcomes)); raise only
`TargetConfigurationError`, and only for a broken process configuration (see
[`TargetConfigurationError`](#neutral-contract) above). `serve_target(server,
host, port)` binds the HTTP transport on a loopback host.

<a id="turn-entrypoint"></a>
### One turn

At instance load, the Lab launcher calls `build(context) -> AgentFactory` once
(the module and attribute named in the launch file's `agent`; see
[Launch file](#launch-file)), then the neutral layer awaits
`factory(view) -> TurnAgent` once per instance, on that instance's own event
loop. From there the layer drives one turn per incoming message:

1. `open(public, *, session_id, seed)` receives the case's `public` mapping,
   the connector's own `session_id`, and the seed the layer computed for this
   repeat from `seed_mode` (see [exact seed](#neutral-contract) above) — the
   agent reports no `effective_seed` of its own. It returns the session's
   initial state (seam 2). If `open` raises, the session is closed and the
   server answers `internal`.
2. `turn(message, state, *, session_id, seed) -> AgentTurn` receives a
   `Message` with `content` (a string) and `fields` (a mapping) and the
   current session state, runs exactly one agent turn and returns its trace
   (seam 3). With a `static` environment, the first message's `content` is
   `public[input_text_key]` and `fields` is the whole `public` mapping; with a
   `scripted` environment, messages come from the case's `private.script`
   entries. See the [evaluation inputs guide](../evaluation-inputs/SKILL.md#corpus).
3. `close_session(session_id)` releases the session's state when the case is
   done; `aclose()` releases the instance.

The agent's own `turn` is what the neutral layer wraps; its input and output
shape determine the case `public` fields and how the scorer reads the answer.
Items: {req:agent.entrypoint}, {req:agent.request_contract}.

<a id="turn-outcomes"></a>
## Turn outcomes

`AgentTurn.outcome` (core a16, CORE-W19-01) is one of three values, and each
one has a different campaign consequence:

- **`ok`** — the agent answered. The turn counts and resets the campaign's
  consecutive-failure count (below).
- **`candidate_failed`** — the candidate is at fault. The sample **counts**
  against the candidate: its events reach the scorer and the editor exactly
  as an `ok` sample's do, and the sample is **never retried**. Consecutive
  `candidate_failed` turns are counted; reaching the campaign manifest's
  `campaign.budget.max_failed_turns` ({req:run.failures}, a required key)
  ends the rollout, and any `ok` turn in between resets the count to zero.
  Below the threshold, a `candidate_failed` turn stays in the sample's
  history with its reason and spends `max_turns` like any other turn — it
  does not by itself end the rollout.
- **`target_failed`** — the target could not answer. The sample is
  **indeterminate**: it carries nothing to any role (no turns, no events, no
  reason — there is no separate journal record for it either), may be
  retried up to `/campaign/sample_retries` times, and never affects a
  candidate's score.

**H1 rule: when in doubt, it is `target_failed`.** `candidate_failed` is
reserved for a **deterministic** defect — one the agent would reproduce on
the same inputs (a broken reply format, an exhausted internal step limit). An
agent-internal timeout is always `target_failed`, never `candidate_failed`:
choosing a deadline does not make wall-clock time deterministic, and a flaky
provider would otherwise be free to move the champion. Core cannot check this
distinction from outside the target, so it is a contract on the binding, not
something enforced at the wire. When a binding cannot tell a deterministic
defect from a timeout or a flaky dependency, it reports `target_failed`: that
costs one indeterminate sample and never a wrong candidate score. See
[Template bindings](#template-bindings) for the same rule stated as a binding
obligation.

**Empty answers in the Lab turn layer.** For `/2` and `/3`, an `ok` turn
whose text is empty after `strip()` becomes `target_failed` with
`TurnReason("lab", "empty_answer")`, even if it has events. Lab replaces any
agent reason before fitting the response budget, preserving events and usage;
`candidate_failed` and `target_failed` remain unchanged. This changes the
meaning of measurements that previously counted empty `ok` turns as answers.
Only `/3` can opt out with `"turn": {"empty_answer": "ok"}`. Command-form `/1`
and turns outside the Lab layer are outside this automatic policy.

**Turn reason.** `AgentTurn.reason`, a `TurnReason(category, code)`, is an
optional, short, structured statement of why the turn ended as it did —
never free text. Both `category` and `code` follow the identifier grammar
`[A-Za-z0-9._-]{1,64}`. It is allowed on **any** outcome, including `ok`: a
harmless annotation does not require inventing a fault to attach it to. Core
carries the reason unchanged to the scorer's trajectory and to the editor's
brief (`edit_brief/4`, or `edit_brief/5` under sequential attempt feedback,
for `/15`; `edit_brief/6` or `/7` respectively for `/16`);
it does **not** reach the reflector (role boundary, PDR-0008) and it never
reaches an `indeterminate` sample's non-existent record, since that sample
carries nothing at all.

<a id="agent-outcome"></a>
### What the agent tells a probe: `agent_outcome`

A probe session (`lab probe say`, see the [connect
scenario](../scenarios/connect/SKILL.md#check)) is the one Lab path that sends
a user message to the target and shows the reply to a person. Besides the
core `outcome`, the probe turn carries a ready field `agent_outcome` from one
neutral vocabulary, so that every client reads the same thing and nobody has
to know a template's own codes:

| `agent_outcome` | When |
|---|---|
| `answered` | `outcome: ok` and `reason == {category: "agent_outcome", code: "answered"}` |
| `off_topic` | `outcome: ok`, `reason.code == "off_topic"` of that category |
| `handoff` | `outcome: ok`, `reason.code == "handoff"` (the agent passed the user to a human) |
| `gave_up` | `outcome: ok`, `reason.code == "gave_up"` |
| `error` | `outcome: candidate_failed` (a deterministic defect of the agent), any `reason` |
| `infrastructure` | `outcome: target_failed` (agent timeout, provider, an indistinguishable failure), or a turn Lab did not bring to an answer from the target |
| `unspecified` | `outcome: ok` without a `reason` of category `agent_outcome`, or with an unknown code |

A template declares only the first four. `error` and `infrastructure` are
derived from `outcome`, so a failure of the target or the provider is never
shown as the agent's fault (H1 rule above).

To declare one, return it from `TurnAgent.turn`, also on a successful turn:

```python
from evolution_lab.target import AgentTurn, TurnReason  # inside TurnAgent.turn

return AgentTurn("ok", answer_text, events=events,
                 reason=TurnReason("agent_outcome", "answered"))
```

`AGENT_OUTCOME_CATEGORY` (`"agent_outcome"`) and `AGENT_OUTCOMES` (the four values a
template declares; Lab assigns `error`, `infrastructure` and `unspecified`)
are exported from `evolution_lab.target`. The declaration is
optional ({req:agent.outcome_vocabulary}); an agent that omits it still
works, its probe turns are `unspecified`.

It means nothing to campaigns and evaluation: core carries a `reason` on `ok`
as it does any `reason`, but neither Lab nor core reads the `agent_outcome`
category there, and it changes no score or count.

**Instance description.** `TurnAgent.describe()` (core a16, CORE-W19-03) is a
one-time statement about how this instance assembled itself — a
configuration hash, where settings came from, a hermeticity mode, where a
seed went — never an observation of a turn. It is a **flat** object of
scalars (`str | int | float (finite) | bool | None`), keys matching the
identifier grammar, at most 32 keys and at most 4096 bytes of canonical JSON;
`_backend` narrows it with core's own `validate_instance_description` and
fails the instance's creation on any violation (see
[Neutral turn contract](#neutral-contract)). It is captured **once**, at
instance creation, and written to that instance's `instance_attested`; a
later read does not re-call it or compare it. It is **not identity** and it
is shown to **no role** — not the editor, not the reflector — precisely so
that a role cannot optimize against reading its own configuration hash. A
literal value of any launch secret inside it refuses the instance load
(`_backend`'s scan is a backstop); keeping secrets out of the returned
mapping in the first place remains the agent's own obligation, since neither
Lab nor core knows which values are secret except by the literal strings the
launch file declared.

**Do not echo role or scorer environments.** `describe()` must not repeat
values from any role or scorer environment: scorer `allowed_env`, chat-role
credentials and proxies, forwarded subprocess values, or the entire
`agent_cli` environment. This includes model names, URLs and any strings of
**8 characters or more** from those environments, even when you do not
consider them secrets. Express settings identity through a digest such as
`settings_sha256`, not raw settings. For example, if a judge's model name
equals the agent's model name, returning `{"model": agent_model}` echoes a
scorer environment value; return the settings digest instead. This is a
connector requirement regardless of which literal values the current pin
detects. Never rely on passing one secret scan as proof the description is
safe.

**Probe timeout:** `turn_timeout_seconds` must be greater than the agent's
own turn limit. Lab cannot infer that limit from the target's settings;
otherwise every long turn aborts the whole probe target, not just the
session. On timeout or cancellation Owner immediately marks the session
`uncertain`, returns without waiting for agent cleanup, and aborts the
target in the background. Other opening sessions do not delay the abort.
An unconfirmed process-group stop **or surviving mutating I/O** keeps the
target quarantined (`cleanup_required`); a requested stop is not confirmed
cleanup. Use `lab agent stop` to resolve cleanup and start the target again
for the next session only after cleanup is confirmed.

**Fixed evaluation's threshold.** Its own `max_failed_turns` resolves to
`max_turns` when omitted in a new preparation. Failure settings are never read
from the campaign manifest, including on the campaign Bundle path. Explicit
settings override Lab's `retry` default; see [Failures during execution](../campaign-package/SKILL.md#run-failures)
and [Evaluation budget](../evaluation-inputs/SKILL.md#evaluation-budget).

**Parallel turns, not yet.** Two sessions of one instance cannot run turns at
the same time (see [Neutral turn contract](#neutral-contract) above,
"Turns are sequential per instance"); lifting that is CORE-W19-04, a
separate core delivery still pending. The neutral layer needs no change of
its own when it lands — it holds no lock of its own: it runs each instance on
one thread and one event loop and relies on core's instance lock to
serialize the turns, and a shared/exclusive split of that lock is core's own
concern.

## Interpreter in role commands

On Core `2.0.0b22`, as already on b9,
scorer, editor, reflector and grouping commands run by their declared absolute spawn path. A venv `bin/python` symlink keeps its
venv packages: the #272 wrapper workaround is no longer needed. Core keeps
this spawn path separately from the interpreter's realpath and seals both;
Lab continues to pin the real executable and declared proof files.

Core refuses with `program_inside_seed` if a command's spawn path, its
directory, realpath, `cwd` or any proof path lies inside the seed. This
includes wrappers in the seed and `<seed>/.venv/bin/python`, even when the
symlink resolves outside it. Declare the interpreter and command materials
outside the seed.

An unfinished run with an interpreter inside the seed cannot continue on b9 or later.
Finish or stop it with its producing pair, then start a new run with an
external interpreter and fresh state. Lab has no linked-run continuation:
`linked_from` is refused as `linked_run_unsupported`.

Core still refuses `python -m …` at bind time: give the script path instead.
The target launch (`lab-target-launch/2` and `/3`) uses its own launch contract.

<a id="launch-file"></a>
## Launch file `lab-target-launch/2` and `/3`

The launch file tells Lab how to start the target process. Its key set is
closed:

| Key | Meaning |
|---|---|
| `schema` | `lab-target-launch/2` |
| `agent` | `{module, attribute, import_root}`. Lab imports `module` (a dotted, installed-package name) and calls its `attribute` as `build(context) -> AgentFactory` (see [Neutral turn contract](#neutral-contract)). `import_root` is `null` when the adapter is installed into the target environment, or a Bundle-relocated directory Lab puts first on `sys.path` before the import. |
| `environment_ref` | Digest of the target environment descriptor; Lab runs `<that environment's interpreter> -I -m evolution_lab.target.launch`. |
| `program_paths` | Files of the agent adapter, relocated into the Bundle (only needed when `import_root` is set). |
| `env` | Non-secret environment values; each must equal the Bundle's declared `env`. |
| `secret_names` | Names only; a subset of the Bundle's secret names. Values are supplied at target start. |
| `endpoint` | `{base_url}`; must equal the manifest's `/target/base_url`. |
| `identity` | `{target_id, contract_digest, settings_hash, runtime_ref_digest, seed_mode}`. `target_id` and `seed_mode` are nonempty strings; the three digests are 64 lowercase hex characters or `null`. Preparation accepts `null`, but Lab's launcher passes the identity to core's `TargetServer`, which refuses a `null` digest, so a launch `/2` target needs all three digests. |
| `policy` | Path of the document carrying the artifact policy (the manifest, or the evaluation input on the evaluation path). |
| `limits` | `{max_instances, max_concurrent_sessions, max_input_bytes, max_output_bytes}`, positive integers. See [Capacity](#capacity). |
| `startup_timeout_seconds` | Positive number: how long target start may take. |
| `stop_grace_seconds` | Positive number: grace period on stop. |
| `conformance` | `null`, `{"kind": "core-a5-session-public/1"}` or `{"kind": "core-session-public/1"}`. See [Conformance mode](#conformance). |

`lab-target-launch/3` adds two required keys to this `/2` table:
`reflector_probe` (null or the campaign probe launch object) and
`turn: {empty_answer: "target_failed" | "ok"}`. It uses the same launcher and
`lab-target-runtime/2`; `/2` keeps its closed key set and defaults to the
empty-answer protection above. The campaign scaffold writes `/3`; the
interactive target-entry probe scaffold stays `/2`. See the
[probe launch reference](../campaign-package/SKILL.md#reflector-probe) for the
consistency table, required fields, restart budget and b13/b14 boundary.

`lab-target-launch/1` continues to be accepted: it declares `command`
(`{executable, args}`) instead of `agent`, for a custom launcher process that
does not run `evolution_lab.target.launch` itself. A custom launcher still has
to speak the same runtime configuration and ready report; Lab's own launcher
runs the `/2` and `/3` forms. Items: {req:launch.environment},
{req:agent.entrypoint}.

At preparation, `endpoint.base_url`, `identity.seed_mode` and the manifest's
declared `contract_digest`/`settings_hash` must agree with the manifest
`/target` section; `max_input_bytes` must be at least the manifest's
`min_input_bytes` and `max_output_bytes` at most its `max_response_bytes`. The
target environment should carry the same `evolution-lab` version that built
the Bundle; preparation warns (`lab_version_mismatch`, with the expected and
found version) when it does not, but does not refuse — the target still
starts, and a resulting failure is worth checking against this warning first.

### Lifecycle and health

Target start and stop are explicit Owner actions; nothing starts a target
implicitly. On start, Lab runs the launch command — for `/2` and `/3`, always
`<environment interpreter> -I -m evolution_lab.target.launch` — with exactly
the declared `env` and the supplied secrets, passes the process a runtime
configuration (`lab-target-runtime/2`, a closed extension of `/1` that adds
`agent` and `secret_names`) and waits, within `startup_timeout_seconds`, for
one ready report (`lab-target-ready/1`). It then reads the target's health
and requires it to match the launch identity and limits.
`examples/owner/evaluation.py` shows how an owner-level caller drives this
lifecycle around the Lab launcher. Lifecycle details are in the
[Owner guide](../owner/README.md).

<a id="execution-policy"></a>
## Target under an execution policy

The optional [execution policy](../budget/SKILL.md) lives outside the Bundle.
In the launch `env`, configure your adapter's model URL as
`http://127.0.0.1:8790/target/v1` and model as the policy route's
`client_model` (for example `target-client`). Those environment setting names
belong to your integration. Declare the policy's `target_token_name` in launch
`secret_names`; Lab fills it, so do not pass its value yourself. The gateway
rewrites the client alias to the policy's upstream `model` and sends no
upstream authorization for `api_key_secret: null`.

Review and confirm the policy with its expected digest, then start the target
using `start_target(..., execution_policy=digest)` or
`lab agent start CONNECTION --execution-policy DIGEST --wait`. Supply only
upstream keys and ordinary target secrets through the usual private secrets
input; upstream keys reach the gateway only. Lab runs the gateway with the
Owner interpreter and `gateway` extra, in a helper scope tied to the target,
then waits for target readiness. Stop/cleanup covers both scopes. No policy
means no Lab gateway and the existing launch behavior.

A suspended campaign can change routes only after target stop/cleanup →
review and confirm a new policy → start a target of the same Bundle → resume.
The complete gateway block and policy presence stay fixed. Stable client
URLs and aliases preserve the Bundle and Core identity; Core does not see the
route change. Lab checks neither served-model nor quantization equivalence;
the human is responsible for it. Keep `reasoning_effort` in the manifest and
put routed-role provider pins in the policy. See the
[complete policy and routing audit](../budget/SKILL.md), including shared uses,
unavailable ledgers and the operator-run budget API that remains supported.

<a id="template-bindings"></a>
## Template bindings

A binding connects one agent framework to the neutral contract above; it
ships with that framework, not with Lab, and Lab does not import it. Whatever
framework it binds:

- It builds only on the neutral contract's public API (above) and its own
  framework's own public API — nothing private to either side.
- It builds the agent from `AgentView.root`, the server-owned read-only
  snapshot of the candidate tree, never from the live source tree.
- It translates its own execution trace into core's event vocabulary (the
  five visible types, plus its own invisible ones) inside `AgentTurn.events`,
  in order.
- It collapses its own outcome vocabulary into the neutral contract's turn
  outcomes by who owns the failure (see [Turn outcomes](#turn-outcomes)):
  `outcome="candidate_failed"` only for a deterministic defect the agent
  would reproduce on the same inputs; `outcome="target_failed"` for an
  agent-internal timeout and for any failure it cannot tell apart — when in
  doubt, `target_failed`. An outcome code the binding does not recognize
  goes to `outcome="target_failed"`, never silently to `"ok"` and never as a
  raised exception the layer has to guess at.
- It returns `state=KEEP` on a failed turn when its framework drops state on
  failure and expects the previous state kept, rather than inventing a state.
- It never puts a secret value in `describe()`.
- It raises `TargetConfigurationError` only when the process configuration is
  broken and every sample would fail — never for one session's or one turn's
  failure.
- It does not put a value in `describe()` that does not reproduce across
  processes (a per-process fingerprint, for example).
- It depends on `evolution-lab`, and through it on Lab's exact core pin; the
  target environment needs the same `evolution-lab` version that built the
  Bundle — see the `lab_version_mismatch` warning in
  [Launch file](#launch-file) above.

Tests and checks for a specific binding live with that binding, not in Lab.

<a id="capacity"></a>
## Capacity

`limits.max_instances` and `limits.max_concurrent_sessions` state how many
instances the target can hold and how many sessions it serves at once. Declare
what the agent really supports; do not assume one session per instance. Both
values enter the campaign identity, so changing them later starts a new
campaign. The core target server advertises `max_concurrent_sessions` in its
capabilities but does not enforce it: the backend itself must stay correct
under that many concurrent sessions. `max_input_bytes` and `max_output_bytes`
bound one request and one reply. See
[Neutral turn contract](#neutral-contract) for how one instance serializes its
own turns. Campaigns can use multiple instances. Fixed evaluation uses one
instance, with concurrent sample scheduling bounded by `max_concurrent_sessions`;
it does not make turns on that instance run simultaneously.

How many instances a campaign needs, and which campaign settings lower that
number, is in [Campaign scale](../campaign-package/SKILL.md#target-capacity).

Items: {req:target.capacity}, {req:agent.session_state}. See also the
[pitfalls guide](../pitfalls/SKILL.md).

<a id="conformance"></a>
## Conformance mode

`conformance` in the launch file declares whether the target offers a free
diagnostic path:

- `null` — no conformance declaration; runtime conformance is not available.
- `{"kind": "core-session-public/1"}` — the integration commits that the
  complete public core conformance sequence runs offline, without model spend.
  Only this declaration authorizes runtime conformance.
- `{"kind": "core-a5-session-public/1"}` — historical token; still readable,
  but it does not authorize runtime conformance.

The declaration is a commitment by the integration, not evidence that
arbitrary integration code is free. Conformance is never run automatically;
a person requests it explicitly. Item: {req:target.conformance_mode}. Reading
its result is covered by the [diagnostics guide](../diagnostics/SKILL.md).
