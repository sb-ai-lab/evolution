# A tour of the examples

[Русский](ru/examples.md)

**Goal:** see a complete, minimal agent wired into Lab, and know which file
does what before you connect your own. Lab ships two runnable examples. Both
are offline: no model, no network, no key, and each finishes in a few seconds.
This page does not repeat their commands; it tells you what they consist of
and where to look.
From a checkout, `just demo` and `just demo-campaign` run them and keep
their workspaces in `.lab/demo/`; [The demo](demo.md) explains what they print.

| Example | What it does | Command and expected output |
|---|---|---|
| [`examples/owner/evaluation.py`](../../lab/examples/owner/evaluation.py) | Measures the project skeleton's stub agent with explicit Owner calls: Bundle → conformance → fixed evaluation → report | [Install, step 5](install.md#step-5-run-the-offline-example) |
| [`examples/owner/campaign.py`](../../lab/examples/owner/campaign.py) | Lets Core improve an agent: Bundle → check → campaign → report → diff → export | [Run a campaign, step 1](campaign.md#step-1-run-the-offline-example-campaign) |

Both are written for the current version pair, `evolution-lab 0.0.3.dev81`
with exactly `evo-skills 2.0.0b22`. The other examples in
[`examples/`](../../lab/examples/README.md) belong to older pairs and are kept for
reading only.

## Where to look, in order

1. **The project skeleton.** `lab new-project DIR` writes it; `lab evaluate DIR`
   measures it. It is where your own agent starts: the agent and the
   adapter in two files, a corpus, a scorer. Its `README.md` says what to
   edit.
2. **`evaluation.py`** — the same skeleton, measured with explicit Owner
   calls instead of `lab evaluate`. Read it when you want to drive Lab from
   your own code.
3. **`campaign.py`** — a campaign, plus an Editor and a Reflector, so the
   Core can change the agent's files and keep the change only if the score
   allows it. It starts from the same project skeleton, with the offline observe profile and a synthetic agent that needs the sample edit to answer correctly.
4. **A realistic agent** — the order-lookup assistant with one tool, in
   [Connect your agent](connect-agent.md#complete-example-the-order-lookup-assistant).
   It shows what the example agent leaves out: settings, tools, failures
   mapped to turn outcomes.

## The evaluation example

> **New in 0.0.3.dev72.** The evaluation example builds its project from the
> project skeleton of `lab new-project`. On 0.0.3.dev71 and earlier it builds it
> from Core's scaffold; read this page at tag `v0.0.3.dev71`.

### What it writes

The script writes the project skeleton into `--source-directory`, fills in
its `"lab:auto"` fields in a run copy, freezes that copy into a Bundle inside
`--state-directory`, and drives it with the Python Owner. The project is the
same one `lab new-project` writes:

```text
example-source/
  agent/my_agent.py        the agent: a stub without a model; in your project, your agent
  agent/adapter.py         the adapter Lab loads: build(context) and five methods that call MyAgent
  seed/skills/SKILL.md     the prompt the adapter reads from the measured copy
  corpus.jsonl             three cases, q1 to q3: public question and oracle.expected
  scorer/score.py          reads one sample on stdin, prints 1.0 if the answer equals the expected text
  launch.json              how Lab starts the adapter (lab-target-launch/3, "lab:auto" fields)
  evaluation-input.json    what to measure: corpus, scorer, metric, seeds (lab-evaluation-input/3)
  README.md                what to edit, field by field
  .lab/run/                the run copy with "lab:auto" filled in; the Bundle is made from it
```

`just demo` uses the same skeleton through `lab new-project` and
`lab evaluate`; [The demo](demo.md#the-agent-the-adapter-and-what-you-replace)
shows which file you replace.

### The agent and the adapter

The skeleton keeps them apart. The agent, `agent/my_agent.py`, knows nothing
about Lab:

```python
class MyAgent:
    def __init__(self, prompt: str) -> None:
        # The prompt is seed/skills/SKILL.md as Lab hands it over. The stub uses
        # it for one thing: whether to answer with the code word alone.
        self._terse = "code word only" in prompt.lower()

    def answer(self, question: str) -> str:
        words = question.lower().replace("?", " ").split()
        code = next((_KNOWN[word] for word in words if word in _KNOWN), "unknown")
        return code if self._terse else f"The code word is {code}."
```

`_KNOWN` holds the code words for `alpha` and `beta`; `gamma` is missing on
purpose, so the measurement shows both a 1 and a 0. The prompt says "code
word only", so the stub answers `A1`; without that sentence it answers
`The code word is A1.`, which the exact-match scorer counts as wrong.

The adapter, `agent/adapter.py`, is what Lab imports. Its parts:

- `build(context)` is the entry point named in `launch.json`
  (`"agent": {"module": "adapter", "attribute": "build"}`). Lab's own
  launcher imports it; you never write an HTTP server.
- `factory(view)` inside it makes one agent per instance. `view.root` is the
  measured copy of `seed/`; the factory reads `skills/SKILL.md` from there and
  creates `MyAgent` with it.
- `open` starts a session and marks a conformance session, `turn` answers one
  message (a fixed text for conformance, `MyAgent.answer` otherwise),
  `close_session` and `aclose` release resources.
- Failures of the agent become turn outcomes: `candidate_failed` or
  `target_failed`, with a short reason code.
- `describe()` is what the agent reports about itself. It must not contain
  secrets.

[Connect your agent](connect-agent.md) explains each method, the outcomes a
turn can return and what to change for your own agent.

### What the script calls

Read `evaluation.py` in two halves:

- `_prepare` builds the project and freezes it: `new_project`,
  `describe_environment`, `resolve_project` (fills in `"lab:auto"`),
  `PreparationStore.create_bundle`, and `prepare_evaluation` with all cases.
- `_run` opens the Owner and goes through the lifecycle: `bind` → `start_target` →
  `conformance` (which stops that Target Service) → a second `start_target` → `start`
  of the evaluation → `report` → `stop_target`.

[What `lab evaluate` does inside](first-evaluation.md#what-lab-evaluate-does-inside)
explains each call in that order. `lab evaluate` makes the same calls and adds
secrets, run files and a readout.

The identity digests in the run copy are the defaults `resolve_project`
computes from the launch declaration; see
[Identity digests](connect-agent.md#choose-and-record-the-identity-digests).

## The campaign example

### What it writes

```text
campaign-source/
  agent/my_agent.py        synthetic demo: answers correctly after the sample edit
  agent/adapter.py         the ordinary skeleton adapter
  evaluation-input.json    fixed measurement of the same project
  campaign-launch.json     how Lab starts the campaign target; policy is manifest.json
  launch.json              evaluation target; policy is evaluation-input.json
  manifest.json            Core's campaign manifest (evolution-v2-manifest/15)
  corpus.jsonl             the cases
  scorer/score.py          the skeleton scorer: compares the whole answer with oracle.expected
  editor/edit.py           Core's sample Editor: rewrites the first line of SKILL.md
  reflector/reflect.py     Core's sample Reflector: diagnoses failing cases for the editor's brief
  seed/skills/SKILL.md     the file the campaign is allowed to change
```

The campaign uses the same `lab new-project` skeleton. The demo replaces the stub with a synthetic agent that knows all three code words but answers them only when the measured skill starts with `IMPROVED:`. The adapter, corpus, scorer and sample roles stay the same. The observe gate is specific to this free wiring demo.

### What happens

The seed's first line scores `0.0`. The sample Editor rewrites it to
`IMPROVED: reference skill`, the agent starts returning the expected code words, and the scorer gives `1.0`. Round 1 ends `promoted` and the Champion becomes `v1`;
round 2 has nothing left to improve and ends `clean`. `diff` and `export` then
show and hand over the changed file.

The script applies the offline demo's observe profile to the project manifest (see
[The campaign demo](demo.md#just-demo-campaign-improving-an-agent)).
It sets `campaign.gate.mode: "observe"`, so no gate is applied: the promotion
shows that the campaign runs end to end, not that the change is better. For
evidence of quality, see the gate settings in [Run a campaign](campaign.md).

### What the script calls

`_prepare` builds the project skeleton, applies the demo profile and resolves `campaign-launch.json`,
then calls `create_bundle` and `prepare_campaign`. `_run` makes these Owner
calls: `bind` → `start_target` → `check` → `start` → wait for the campaign →
`report` → `diff` → `export` → `stop_target`. Steps 7–10 of
[Run a campaign](campaign.md) are the same calls with your own inputs.

## Reading the output

Each script prints one JSON summary. The fields to check and their expected
values are in
[Install: What success looks like](install.md#what-success-looks-like) for the
evaluation, and in
[Run a campaign: What a finished campaign looks like](campaign.md#what-a-finished-campaign-looks-like)
for the campaign. Reports, corpus rows and conversations stay private in the
state directory.

## See the results in the panel

The examples do not start the [panel](panel.md): they call the Owner library
directly and exit when they are done. The finished run stays in the state
directory, so you can open it afterwards. From `lab/`, once the script has
printed its summary, start the service over the example's state, the
`--state-directory` you passed (here `campaign-state`, as in
[Run a campaign](campaign.md#step-1-run-the-offline-example-campaign)):

```bash
.lab/venv/bin/lab serve --home "$PWD/lab-home" --state "$PWD/campaign-state"
```

In a second terminal, from `lab/` as well:

```bash
.lab/venv/bin/lab panel --home "$PWD/lab-home" --open
```

Both paths must be absolute, hence `$PWD`. Open *All experiments* and click the
campaign row; the screens are described in
[The Lab panel, step 4](panel.md#step-4-open-the-screens). To look at the
evaluation instead, pass its state directory, such as
`--state "$PWD/lab-state-example"` from
[Install](install.md#step-5-run-the-offline-example). Stop the panel and then
the service with Ctrl+C. The service needs no `--execution-wheel` here: it
only reads finished runs.

## Before you run them

- Run from `lab/` after `just setup`, and
  pass the same interpreter to run the script and as `--python`. A `--python`
  path containing `..`, such as `../lab/.lab/venv/bin/python`, fails with
  `environment_invalid`.
- `--state-directory` and `--source-directory` must not exist yet. After a
  failed run, use new names.
- `--port` must be free on `127.0.0.1`. The example's stand-in agent listens
  there for Core while the script runs; it is not the panel's port.

## Next

- [Run your first evaluation: Measure your own agent](first-evaluation.md#measure-your-own-agent)
  — `lab new-project` and `lab evaluate` on your own agent, corpus and scorer.
- [Connect your agent](connect-agent.md) — the turn contract and the launch
  file in full.
- [Run a campaign](campaign.md) — write your own manifest, editable scope,
  Editor and Reflector.
