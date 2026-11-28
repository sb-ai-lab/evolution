# My agent: measurement and campaign

This project works with both `lab evaluate` and `lab campaign` in Evolution Lab.
It runs as it is, with a stub agent that needs no model. Replace the stub step
by step.

If your agent is a function `answer(question) -> str`, a first measurement needs
edits only in `agent/my_agent.py` and `corpus.jsonl`. `agent/adapter.py` needs
no change: its `TODO(you)` marks are for agents with their own settings,
state or errors.

In the Lab checkout, `lab` is not on your PATH: use `just new-project DIR` and
`just evaluate DIR` (or `just lab ...`) where this page writes `lab ...`.

## What to edit, in this order

1. `agent/my_agent.py`: the agent itself. In a real project this file imports
   your agent and wraps it so that `answer(question)` returns the answer text.
2. `agent/adapter.py`: connects `MyAgent` to Lab. Leave it for a first
   measurement of a plain `answer(question) -> str` agent. Every place to edit
   is marked `# TODO(you):` (settings and secrets, `describe()`, failure
   outcomes).
3. `corpus.jsonl`: your questions (`public.text`) and the expected answer
   (`oracle.expected`). One line per case; fields below.
4. `scorer/score.py`: what counts as a correct answer. The stub checks that the
   whole answer equals the expected text (after `strip()` and case-folding), so
   "50" does not match "150". Its docstring says how to accept a substring or a
   number.
5. `seed/skills/SKILL.md`: the prompt of your agent, the part a campaign can
   change later. The adapter reads it from `view.root` and hands it to
   `MyAgent`. The stub answers with the code word alone only while the prompt
   says "code word only"; edit the prompt and the answers change. On each real
   turn the adapter reports `skills/SKILL.md` in `TurnProvenance.used`, relative
   to the measured tree root. When replacing the stub, update that report to
   name only the candidate files or components your agent actually used.
6. `launch.json` and `evaluation-input.json`: change only the fields marked
   "yes" below.

Leave `"lab:auto"` as it is: `lab evaluate` and `lab campaign` compute these values on each run
and writes them into its copy of the project, not into these files. Other Lab
tools do not understand `"lab:auto"`; to use the files without those commands,
take the filled-in copy from `.lab/runs/NNN/source/`.

## `corpus.jsonl`: one JSON object per line

| Field | What a first project puts there |
|---|---|
| `case_id` | A unique name for the case (`q1`, `u2`). |
| `group_id` | Any string. One group for all cases, as here (`codes`), is fine for a first measurement: groups matter when a campaign compares two versions, which weights every group equally (section "Groups and strata" of the guide below). Before you compare versions, give independent scenarios their own groups and keep paraphrases of one question in one group. |
| `stratum` | Any string naming the kind of situation (`known`, `unknown`); results are reported per stratum. |
| `public.text` | The question, exactly as the agent receives it. Never the expected answer. |
| `oracle.expected` | The expected answer; only the scorer sees it. |
| `oracle.critical` | `true` marks a hard constraint (one failure rejects a version in a campaign). The stub scorer always passes it, so leave it as it is. |

The full rules are in the guide "Evaluation inputs" (`docs/guide/evaluation-inputs.md`,
[online](https://github.com/sb-ai-lab/evolution/blob/main/docs/guide/evaluation-inputs.md#cases-and-the-corpus)).

## Measure it

```sh
lab evaluate . --wheel LAB_WHEEL --wheel CORE_WHEEL
```

Run it from this folder. `LAB_WHEEL` and `CORE_WHEEL` are the wheel files you
installed Lab and the core from; or, in the Lab checkout, run
`just evaluate DIR`, which passes them. Add
`--python PATH` if your agent runs in another virtualenv with the same Lab
installed. Lab writes everything into `.lab/` here: open
`.lab/latest/conversations.md` first. Exit code 0 means the measurement was
accepted; 1 that it was not; 2 a problem in these files; 3 a target that
would not start, or a Lab or environment failure. If the target would not
start, the readout shows the last lines your agent's process wrote (usually
an import error in `agent/adapter.py`, or a variable missing from
`launch.json` `env`) and the path of the full log.

Run it once before you change anything: the stub scores 1, 1 and 0 (it does
not know the third code word). The last line says both things apart: the
measurement is accepted (every sample was scored) and the mean score is 0.67
(2 of 3 scored 1).

## Paid models

A first measurement runs every case in `corpus.jsonl` once. If your agent calls
a paid model, keep only a few cases in `corpus.jsonl` for the first run, and add
the rest after you have read the first result.

## `launch.json`

| Field | Do you edit it | What goes there |
|---|---|---|
| `schema` | no | `lab-target-launch/3` |
| `reflector_probe` | no | `null` |
| `turn.empty_answer` | rarely | `target_failed` makes an empty answer a target failure; `ok` accepts it |
| `environment_ref` | no | `lab:auto`: digest of the Python environment |
| `agent.module`, `agent.attribute`, `agent.import_root` | only if you rename the adapter | `adapter`, `build`, `agent` |
| `program_paths` | if you add code folders | folders copied into the measured version, `["agent"]` |
| `env` | yes | non-secret variables your agent reads; the target gets nothing else |
| `secret_names` | yes | names of secret variables (keys); values come from your environment at run time |
| `endpoint.base_url` | no | `lab:auto`: a free local port, the same as `target.base_url` |
| `identity.target_id` | optional for a first run | a short name for your agent; the default works |
| `identity.contract_digest`, `identity.settings_hash`, `identity.runtime_ref_digest` | no | `lab:auto` |
| `identity.seed_mode` | rarely | `none` for most model-backed agents |
| `policy` | no | `evaluation-input.json` |
| `limits` | if your agent allows more | instances, concurrent sessions, input and output bytes |
| `startup_timeout_seconds`, `stop_grace_seconds` | if your agent starts slowly | seconds |
| `conformance` | rarely | `{"kind": "core-session-public/1"}` if the adapter answers the self-test without the agent, `null` otherwise |

## `evaluation-input.json`

| Field | Do you edit it | What goes there |
|---|---|---|
| `schema` | no | `lab-evaluation-input/3` |
| `seed_root` | no | `seed`: the folder holding the prompt |
| `policy` | rarely | which paths a campaign may change (`writable_paths`) |
| `target.base_url`, `target.contract_digest`, `target.settings_hash` | no | `lab:auto` |
| `target.max_response_bytes` | if answers are long | upper bound of an answer in bytes |
| `target.seed_mode` | rarely | the same as `identity.seed_mode` |
| `corpus.path` | no | `corpus.jsonl` |
| `environment` | no | `static`: each case is one message, taken from `public.text` |
| `scorer.argv` | no | `lab:auto` for the interpreter, then `score.py` |
| `scorer.cwd`, `scorer.dependency_paths` | if the scorer needs more files | scorer folder and extra files |
| `scorer.allowed_env` | if the scorer needs a key (a judge model) | variable names |
| `scorer.timeout_seconds` | if scoring is slow | seconds |
| `evaluation_set_id` | optional for a first run | a name for this set of cases; the default works |
| `metric_plan` | no | `{"primary": "score"}` |
| `defaults.repeat_seeds` | rarely | one entry per sample of each case; `[null]` is one sample |
| `defaults.max_turns` | if cases are conversations | turns per sample |
| `defaults.timeout_seconds` | if a sample is slow | seconds per sample |
| `defaults.max_failed_turns`, `defaults.call_retries` | rarely | failure budgets |

## More help

- The guide "Connect your agent" explains the adapter, the turn outcomes and
  the launch file step by step. It is `docs/guide/connect-agent.md` in the
  Evolution repository (https://github.com/sb-ai-lab/evolution).
  "Run your first evaluation" (`docs/guide/first-evaluation.md`) explains what
  `lab evaluate` prints, its run files and its exit codes.
- The same contract ships with Lab itself: `lab guides path` prints where the
  bundled guides are installed; open `target-connector/SKILL.md` there.


## Improve it with a campaign

`manifest.json` is Core's `/15` statistical campaign scaffold, adapted to this project's agent, corpus, scorer and seed. `campaign-launch.json` starts the same adapter under that manifest's policy; `launch.json` remains the evaluation launch file. `editor/edit.py` and `reflector/reflect.py` are Core's deterministic sample roles. Replace them with your editing and reflection programs when using a real agent; the samples demonstrate file flow, not agent quality.

```sh
lab campaign . --wheel LAB_WHEEL --wheel CORE_WHEEL
```

From the Lab checkout, use `just campaign DIR`. The command fills `lab:auto` only in its run copy, runs conformance, then starts a fresh Target Service for the campaign. Core's gate decides whether a change is promoted. The statistical gate remains enabled in this project; the offline demo alone uses the observe smoke profile.

## Failure tolerance: every number is explicit

Both `evaluation-input.json` and `manifest.json` state the `retry` profile below. Change the numbers in each file before a new run. Evaluation does not inherit campaign settings.

| Key | What it controls | `retry` | `no_retry` | `fail_fast` |
|---|---|---|---|---|
| `defaults.sample_retries`, `campaign.sample_retries` | Retries of an unmeasured sample | 3 | 0 | 0 |
| `defaults.min_coverage` | Minimum scored share for evaluation acceptance | 0.95 | 0.95 | 1 |
| `defaults.max_consecutive_unmeasured` | Consecutive unmeasured samples before evaluation stops | 3 | 3 | 1 |
| `campaign.indeterminate.<owner>.max_count` | Unmeasured tries allowed for target, environment, scorer or rollout | 20 | 20 | 0 |
| `campaign.indeterminate.<owner>.max_rate` | Allowed unmeasured share for that owner | "1" | "1" | "0" |
| `reflector.max_consecutive_failures` | Consecutive Reflector failures allowed | 5 | 5 | 1 |
| `defaults.call_retries.max_attempts`, `campaign.call_retries.max_attempts` | Attempts of a failed target call | 3 | 3 | 1 |
| `defaults.call_retries.turn_max_attempts`, `campaign.call_retries.turn_max_attempts` | Attempts of a failed turn | 2 | 1 | 1 |
| `defaults.max_failed_turns`, `campaign.budget.max_failed_turns` | Failed turns allowed within one sample | `max_turns` | `max_turns` | 1 |

`retry` means resending a question is safe for your agent. Use `no_retry` if resending could repeat an external side effect. `fail_fast` stops on the first failure. These profiles control execution tolerance, not whether a measured Candidate is better. `defaults.max_failed_turns` follows evaluation's `defaults.max_turns`; the campaign value follows `campaign.budget.max_turns`.
