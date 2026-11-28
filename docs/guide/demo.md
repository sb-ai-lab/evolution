# The demo: what `just demo` shows

[Русский](ru/demo.md)

**Goal:** see in two minutes what Lab does with an agent, and know which
files to open afterwards. Two demos run offline on a synthetic agent: no
model, no network, no API keys, a few seconds each. Both keep everything they
made in a workspace under `.lab/demo/`, so you can look at it after the run.
`just demo` is built from the project skeleton you will use for your own
agent, so its files are the ones you will edit.

| Command | What it shows | Workspace |
|---|---|---|
| `just demo` | Lab **measures** an agent as it is (a fixed evaluation) | `.lab/demo/evaluation/` |
| `just demo-campaign` | the Evolution Core **improves** the agent, and Lab runs and measures it (a campaign) | `.lab/demo/campaign/` |
| `just demo-panel` / `just demo-panel campaign` | the same results in the browser panel | — |

Run `just setup` first ([README](../../QUICKSTART.md)). Words used
below: a **Target Service** is your agent running as a local service Core talks
to; a **Bundle** is a frozen, immutable copy of the inputs; **conformance**
and **check** test that the target speaks the turn protocol. See
[How Lab works](concepts.md) for more.

## `just demo`: measuring an agent

`just demo` runs the same two commands you will run on your own agent:

```sh
lab new-project .lab/demo/evaluation/project
lab evaluate .lab/demo/evaluation/project --wheel <Lab wheel> --wheel <core wheel>
```

The demo's project is the **project skeleton**, exactly what
`lab new-project` gives you. What happens, in order:

1. `lab new-project` writes the skeleton into `.lab/demo/evaluation/project/`
   and prints the next steps for your own project.
2. `lab evaluate` copies the project into a run directory and fills in the
   fields marked `"lab:auto"`: the environment digest, a free port, the
   scorer's interpreter and the identity digests.
3. Lab freezes the copy into a Bundle, starts the agent as a target and runs
   conformance: Core sends it a protocol test suite. Lab then stops that
   target.
4. Lab starts a fresh target and runs a **fixed evaluation**: 3 questions × 1
   repeat = 3 samples. Each answer is scored.
5. Lab stops the target, confirms that nothing is left running and prints
   the readout.

### The agent, the adapter and what you replace

The skeleton keeps the agent and the adapter in two files, so you can see
which is which:

| File | What it is | For your agent |
|---|---|---|
| `agent/my_agent.py` | **The agent.** A stub without a model: `MyAgent(prompt).answer(question)` answers with the code word it knows; it answers with the bare code word because the prompt says "code word only". It knows nothing about Lab. | Replace it: import your own agent here. |
| `agent/adapter.py` | **The adapter.** The module Lab loads (`launch.json` → `agent`): `build(context)` and a class with five methods that call `MyAgent`. It also answers conformance without calling the agent. | Keep its shape; edit the places marked `# TODO(you):`. |
| `seed/skills/SKILL.md` | The agent's prompt. The adapter reads it from the measured copy. | Your prompt. |
| `corpus.jsonl` | Three questions, `q1`–`q3`, with the expected code word in `oracle.expected`. | Your questions and expected answers. |
| `scorer/score.py` | Gives **1** when the answer equals the expected text (after `strip()` and case-folding), else **0**. | What "correct" means for your agent. |
| `launch.json`, `evaluation-input.json` | How Lab starts the adapter and what it measures. | Only the fields the skeleton's `README.md` marks "yes"; leave `"lab:auto"`. |

[Connect your agent](connect-agent.md) walks through each of these edits.

### What it prints

```text
Samples: case, question -> the agent's answer [score]
  c-3a6eb0b2… (evaluation)
    q1  What is the code word for alpha? -> A1   [score 1]
    q2  What is the code word for beta? -> B2   [score 1]
    q3  What is the code word for gamma? -> unknown   [score 0]

Lab filled: environment digest, port, scorer Python, 3 identity digests (listed in resolved.json); nothing to do.
run 001: measurement accepted: 3/3 samples scored; mean score 0.67 (2 of 3 scored 1)
```

Before the samples, the readout says what happened, step by step. After
them, it lists the run files and which one to open first.

**The 0 for `q3` is expected.** The stub knows the code words for `alpha` and
`beta`, but not for `gamma`. So you see that the scorer really compares
answers, and the measurement is still accepted: all three samples were
scored. A fixed evaluation records how the agent performs as it is; Lab does
not change the agent during an evaluation. A campaign is what tries to raise
the score: it proposes changes to the agent and measures them (next section).

The last line keeps two facts apart. *Accepted* means every sample was
scored (the run is usable). The *mean score* is the quality: here 2 of 3
answers were correct. The `Lab filled: ...` line names, in plain words, the
fields that were `"lab:auto"`; `resolved.json` lists them with their values,
and you have nothing to do.
The full output, its exit codes and the run files are explained in
[Measure your own agent](first-evaluation.md#measure-your-own-agent).

### The workspace

```text
.lab/demo/evaluation/project/     the project skeleton: the same files you get from lab new-project
  agent/my_agent.py               the agent (a stub)
  agent/adapter.py                the adapter Lab loads
  seed/skills/SKILL.md            the prompt
  corpus.jsonl                    the three questions
  scorer/score.py                 scores one answer
  launch.json                     how Lab starts the adapter
  evaluation-input.json           what to measure: corpus, scorer, metric, repeats
  README.md                       what to edit, field by field
  .lab/
    state-<pair>/                 Lab's internal state: Bundle, runs, logs
    runs/001/
      conversations.md            every sample: question, answer, score, scorer verdicts
      report.md                   Lab's report of the evaluation
      resolved.json               the fields Lab filled in, the Python and the wheels
      summary.json                the facts the command checked
      evaluate.log                the steps of the run
      source/                     the project copy this run measured
    latest -> runs/001
```

Open **`conversations.md`** first. It shows, per sample, what Core asked,
what the agent answered and what the scorer said. **`report.md`** is the same
report you get from `lab report` for any evaluation: coverage (how many
samples were scored out of how many were planned), the mean score and a
table of samples. Its headings are in Russian, as in the auto-evolve app.
[Read the results](results.md#evaluation-reports) explains every line.

`state-<pair>/` is Lab's working directory. Its folders are named by digests
and ids and are not meant to be read by hand. Read them through `lab`
commands or the panel.

## `just demo-campaign`: improving an agent

The campaign demo uses the same project skeleton through [`examples/owner/campaign.py`](../../lab/examples/owner/campaign.py). The adapter, corpus, scorer and sample roles are unchanged. It applies the offline observe profile and replaces `agent/my_agent.py` with a synthetic agent that knows all three code words. That agent answers correctly only when the first skill line starts with `IMPROVED:`.

The campaign may edit `source/seed/skills/SKILL.md`.
What happens:

1. The example writes the project. Next to the agent it adds a campaign
   manifest (`manifest.json`), an **Editor** (`editor/edit.py`) that proposes
   changes to `SKILL.md`, and a **Reflector** (`reflector/reflect.py`) that
   explains failing cases to the Editor.
2. Lab freezes the project into a Bundle, starts the target and runs a check.
3. **Round 1.** Core measures the starting version, the seed `v0`: score 0.
   The Editor rewrites the first line of `SKILL.md` to
   `IMPROVED: reference skill`. Core measures this Candidate: score 1.
   Core promotes it, and the Candidate becomes the Champion `v1`.
4. **Round 2.** Core measures `v1` as the new baseline. There is nothing
   left to improve, so the round ends `clean` and the campaign finishes.
5. Lab reads what changed between `v0` and `v1` and exports the Champion.

The samples show the change in the agent's answers:

```text
  m-r00-champion (baseline, round 1)
    q1  What is the code word for alpha? -> not improved   [score 0]
  m-r01-a01 (attempt, round 1, attempt r01-a01)
    q1  What is the code word for alpha? -> A1   [score 1]
  m-r01-a01-parent (other)
    q1  What is the code word for alpha? -> not improved   [score 0]
  m-r01-final-01-a01 (baseline, round 2)
    q1  What is the code word for alpha? -> A1   [score 1]
```

The four measurements are:

- `m-r00-champion`: the seed `v0`, measured before the first round;
- `m-r01-a01`: the Candidate the Editor made in round 1;
- `m-r01-a01-parent`: the seed measured again next to the Candidate, so the
  two are compared under the same conditions;
- `m-r01-final-01-a01`: the new Champion `v1`, measured as round 2's baseline.

### The workspace

```text
.lab/demo/campaign/
  source/                the agent project the example wrote: agent, launch.json, corpus, scorer, seed
  source/manifest.json   the core's campaign settings: rounds, gate, editable files
  source/editor/         the editor that changes SKILL.md
  source/reflector/      the reflector that explains failures to the editor
  changes.diff           what the campaign changed: v0 -> v1
  export.zip             Lab's export of the champion
```

It also has `conversations.md`, `report.md` and `summary.json` at its top
level, and Lab's state in `state/`.

**`changes.diff`** is the result of the campaign in one file: the one line of
`SKILL.md` the Editor rewrote. **`export.zip`** is the Champion packaged by
`lab export` (format `lab-change-set/1`): `manifest.json` lists each changed
file with its hashes, and `payload/<n>` holds the new bytes. Lab never applies
it; you carry the change into your agent's repository yourself
([Export](results.md#export-the-champion-as-a-zip)).
**`report.md`** lists the rounds, the attempts, why each ended as it did and
every measurement ([Campaign reports](results.md#campaign-reports)).

**The promotion here is not evidence of quality.** The demo runs the
confidence gate in `observe` mode: the gate is not applied, and the report
says so (`observe_gate_not_enforced`). A real campaign enforces the
gate ([Run a campaign](campaign.md)).

## In the panel

```sh
just demo-panel              # the evaluation
just demo-panel campaign     # the campaign
```

The command starts a Lab service that reads the demo's state
(`project/.lab/state-<pair>/` for the evaluation, `state/` for the campaign),
then the panel, and prints a link. Open it and go to *All experiments*, then click
the row ([the panel's screens](panel.md#step-4-open-the-screens)). Ctrl+C
stops the panel and the service. The demo's state is read only and stays
in place.

## Running it again

Each run replaces its workspace. The two demos keep separate workspaces, so
`just demo` does not remove the campaign's. Delete `.lab/demo/` whenever you
like; nothing else uses it.

If `just demo` fails, it prints the reason and exits with the code of
`lab evaluate` ([exit codes](first-evaluation.md#exit-codes)); the run's
`evaluate.log` is in `project/.lab/runs/001/`. If `just demo-campaign` fails,
it prints the end of the example's log and keeps it as `example.log` in the
workspace. See [Install → Troubleshooting](install.md#troubleshooting).

## Next

- Your own agent: `just new-project ~/my-agent-lab`, then
  [Connect your agent](connect-agent.md) — replace the stub agent, questions
  and scorer with your own, and measure with `just evaluate ~/my-agent-lab`.
- [Run your first evaluation](first-evaluation.md): what `lab evaluate`
  prints, its run files and exit codes, and the same flow in Python.
- [Run a campaign](campaign.md): your own manifest, editable files, Editor
  and Reflector.
