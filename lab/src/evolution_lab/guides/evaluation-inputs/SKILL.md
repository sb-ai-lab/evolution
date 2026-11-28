---
name: evolution-lab-evaluation-inputs
description: Build the corpus, environment, scorer and metric that Lab uses to measure an agent.
---

# Evaluation inputs

For the first run from a project directory, use [first evaluation](../first-evaluation/SKILL.md) for the launch and acceptance steps.

A Lab measurement needs four inputs besides the target: a corpus of cases, an
environment that turns a case into messages, a scorer that turns a finished
conversation into metrics, and a named primary metric. They are the same on
both paths; only the document that declares them differs (manifest `/15` for a
campaign, `lab-evaluation-input/1`, `/2` or `/3` for evaluation only).

Examples use a synthetic *order-lookup assistant* with a `lookup_order` tool.

For a Bundle of kind `evaluation`, `launch.policy` must point to the
`lab-evaluation-input/1`, `/2` or `/3` document itself, for example
`"policy": "evaluation-input.json"`, even if a campaign manifest is also
present. A different document is refused as `unsupported_input`, naming
`launch.policy`. During bundling Lab generates the internal conformance
manifest and binds the saved launch to it.

<a id="corpus"></a>
## Corpus

The corpus is a UTF-8 JSON Lines file: one case per nonblank line. Each record
has the fields of core `evolution.tree.execution.interaction.EvalCase`:

| Field | Required | Meaning |
|---|---|---|
| `case_id` | yes | Unique within the corpus. |
| `group_id` | yes | Cases that belong together (for example, variants of one scenario). |
| `stratum` | yes | The slice of the corpus the case belongs to. |
| `tags` | no | List of strings; defaults to empty. |
| `public` | yes | What the agent and the environment may see. |
| `private` | no | What only the environment sees: the `script` of a `scripted` environment and `max_turns`. The agent never receives it; defaults to `{}`. |
| `oracle` | no | What only the scorer sees: expected results and checks; defaults to `{}`. |
| `reflection_metadata` | no | Diagnostic mapping kept beside the case for the reflector, not copied into `EvalCase`; defaults to `{}`. Accepted only when the corpus is loaded on the campaign path (`load_reflection_corpus`, manifest `/15`, including fixed evaluation derived from a campaign Bundle); a `lab-evaluation-input/1`/`/2`/`/3` corpus (`load_eval_corpus`) rejects it with `unknown_key`. Core caps it at 64 keys, 16384 bytes of canonical JSON and nesting depth 8 (`bindings.py`). |

Who sees what:

- **Agent (target)** — `public`: the target's session is opened with the
  case's `public` mapping, and with a `static` environment the message fields
  are that mapping too.
- **Environment** — `public` and `private` (`private.script`,
  `private.max_turns`). Exact Core 2.0.0b22 opens the target's session with `public`
  only, so the agent never receives `private`.
- **World** — simulated data for the injected backends (the example's orders)
  must reach the agent: put it, or a key that selects a fixture, in `public`,
  or keep it in fixture files inside the measured tree (for example under the
  manifest's `dependency_paths`). `public` is also shown to the editor and the
  reflector, so never put expected answers there — they belong in `oracle`.
- **Scorer** — `case_id`, `public` and `oracle`, plus the recorded
  conversation. The scorer does not receive `private`.

A synthetic record:

```json
{"case_id": "lookup-1", "group_id": "lookup", "stratum": "single-order",
 "public": {"text": "Where is order A-100?",
            "orders": {"A-100": {"status": "shipped"}}},
 "oracle": {"expected_status": "shipped", "critical": true}}
```

Items: {req:eval.corpus}, {req:world.data}, {req:world.reference_time}
(when answers depend on "now", give an explicit reference time in `public`
or in the launch file's `env`, never the system clock).

### Environment

The environment decides which messages the target receives:

- `static` — one message whose `content` is `public[input_text_key]` (a
  string) and whose `fields` are the whole `public` mapping. In manifest `/15`,
  `input_text_key` is optional and defaults to `text`; in
  `lab-evaluation-input/1`/`/2`/`/3` it is a required string.
- `scripted` — messages come from `private.script`, an array of
  `{"content": "...", "fields": {...}}` entries (`fields` optional), one per
  turn. `input_text_key` is absent in manifest `/15` and `null` in
  `lab-evaluation-input/1`/`/2`/`/3`.

It is declared at `/environment` in both documents.

### Conformance cases

`public.conformance: true` is reserved for core conformance. Preparation
refuses a campaign or evaluation corpus that contains such a case with
`reserved_public_input`. Whether a target offers conformance is declared in
its launch file; see the
[target connector guide](../target-connector/SKILL.md#conformance).

### Selection and coverage

A campaign measures its whole corpus. Fixed evaluation is prepared with one
explicit selection (`{"all": true}`, `{"count": N}` or an ordered
`{"case_ids": [...]}`); there is no random sampling, and a selection makes no
representativeness claim. After a run, coverage compares the confirmed
schedule with scored samples. Details are in the
[preparation guide](../preparation/SKILL.md) and the
[Owner guide](../owner/README.md). Keep held-out cases out of a campaign
corpus: see the [pitfalls guide](../pitfalls/SKILL.md#test-outside-feedback).

Fixed evaluation judges `candidate_failed` turns with its own resolved
`max_failed_turns` (new omission default: `max_turns`). Campaign settings are
not inherited. Choose matching explicit settings when comparability matters;
see [Evaluation budget](#evaluation-budget).

<a id="scorer-protocol"></a>
## Scorer protocol

The scorer is a subprocess described by a core `CommandSection`:

| Field | Meaning |
|---|---|
| `argv` | Nonempty command line. |
| `cwd` | Working directory of the scorer program. |
| `dependency_paths` | Files and directories the scorer depends on; they are part of its identity. |
| `allowed_env` | Names of environment values forwarded to the scorer; each must be a declared `env` or secret name. |
| `timeout_seconds` | Positive timeout for one scoring call. |

Where it is declared:

- manifest `/15`: `/scorer` with these five fields plus `metric_contract` (see the campaign package guide);
- `lab-evaluation-input/1`/`/2`/`/3`: `/scorer` with `kind: "subprocess"` plus the
  same five fields.

One call scores one sample. The scorer reads one JSON object on stdin:

```json
{"case_id": "...", "public": {}, "oracle": {}, "view": {"history": {}, "terminal": "...", "target": {}}}
```

`view.history` holds the recorded turns (messages and the target's trace),
`view.terminal` the terminal state, `view.target` the target identity. The
candidate tree's path is in `EVO_CANDIDATE_ROOT`. The scorer writes one JSON
object on stdout with these keys only:

| Key | Meaning |
|---|---|
| `metrics` | Required, nonempty mapping of metric name to finite number. |
| `critical_pass` | A boolean when the case's `oracle.critical` is `true`; absent or `null` otherwise. |
| `verdicts` | Optional list of per-criterion verdicts. |
| `public_feedback` | Optional mapping. |
| `details` | Optional mapping. |

If your scorer can distinguish an absent answer from a wrong answer, emit
`{"criterion": "answered", "passed": false}` for no answer and `passed: true`
when an answer is present. Lab counts these verdicts for scored samples in
fixed evaluations and campaign measurements. Missing verdicts produce
`not_reported` or `partial`, not an inferred answer; unreadable evidence is
`unavailable`. The reflector also receives the verdict as an ordinary criterion.

`verdicts` is formally optional. For samples whose execution outcome is `completed`,
if the scorer supplies neither `verdicts` nor `critical_pass`, the campaign focus is
always empty for those samples: metrics do not identify failures for focus. Core
also treats execution outcomes other than `completed` as failures. A campaign of
clean rounds can therefore mean a baseline with no failures, or a scorer that
supplies only metrics for completed samples.

Before a paid campaign, inspect the fixed baseline's `lab measurements` or
`lab report` result. `scorer_without_focus_signal` means every admitted sample
in that evaluation had empty verdicts and `critical_pass: null`; its `admitted`
and `not_completed` counters describe that evaluation. With this scorer,
campaign focus comes only from execution outcomes other than `completed`, not
metrics. It does not predict that every future round will be clean.
`lab_notices_state: unavailable` means at least one admitted sample's scorer
output or journal evidence could not be read; an empty notice list in that
state does not confirm a focus signal. Preparation and `check` do not run the
scorer and cannot establish this evidence.

Since b11, scorer `allowed_env` secrets of 8 characters or more in `public_feedback`
are masked before the Reflector and Grouper read them, and entries containing
them are dropped before the Editor; see [masking and its limits](../pitfalls/SKILL.md#the-scorer-leaks-the-oracle).

A nonzero exit, a timeout or unknown output keys raise a scoring error for
that sample; they are not a score. The scorer program must live outside the
candidate tree.

Expected results and checks live in the case `oracle` (item {req:eval.gold}).
A scorer that calls model judges declares the judge's keys in `allowed_env`
and its program files in `dependency_paths` ({req:eval.judges}).

### Metric

Name one primary metric that every scorer output contains
({req:eval.metric}):

- campaign: `/campaign/metric/primary` in manifest `/15`, for example
  `"score"` (so `/campaign/metric` is `{"primary": "score"}`);
- evaluation: `/metric_plan` in `lab-evaluation-input/1`/`/2`/`/3`, for example
  `{"primary": "score"}`. Fixed evaluation derived from a campaign Bundle
  takes an explicit `metric_plan` at preparation.

In a campaign the primary metric must lie in [0, 1] for every scored case. On core
0.3.0a26 and newer a scored primary outside [0, 1] suspends the campaign
(`scorer_metric_out_of_range`); continuing re-measures the named cases with the same
scorer, and a fixed scorer is a new Bundle and a new campaign. Fixed evaluation does
not require the range.

<a id="evaluation-budget"></a>
## Evaluation budget

Fixed evaluation has its own failure settings; it never reads a campaign's
`/campaign/budget/max_failed_turns`, `/campaign/call_retries` or other failure
keys. Choose the named [failure variant](../campaign-package/SKILL.md#run-failures)
before preparing a plan. All values below describe **new preparation**.

| Field | Fallback when absent or `null` | Limits and meaning |
|---|---|---|
| `sample_retries` | `3` | Integer 0–8; extra attempts for an unmeasured sample. `0` disables sample retries. |
| `min_coverage` | `0.95` | Finite number > 0 and ≤ 1; minimum scored fraction for accepting an evaluation without a stop. |
| `max_consecutive_unmeasured` | `3` | Positive integer N, or `"off"` to disable this stop. Count consecutive unmeasured scheduled samples after sample retries. |
| `max_failed_turns` | resolved `max_turns` | Positive integer; consecutive agent-owned failed turns that end a rollout. |
| `call_retries` | `{"max_attempts": 3, "turn_max_attempts": 2}` | Closed object with integer values 1–10, or `"off"` to disable call retries. |
| `concurrency` | `1` | Positive integer ≤ the launch's `limits.max_concurrent_sessions`; samples share one target instance. |
| `scorer_concurrency` | `1` | Positive integer; simultaneous scoring calls. |

`prepare_evaluation` accepts all these named keyword arguments. Explicit
non-`None` arguments override input defaults; `None` means not given. Missing
or `null` input defaults use the fallbacks above, including for `/1` and `/2`
inputs prepared now. This is the `retry` default, so safe resending of the
question must be established. For an agent whose turn changes the world, choose
`no_retry` explicitly. An explicit `call_retries="off"` overrides input retries.

`lab-evaluation-input/1`, `/2` and `/3` remain accepted with their closed shapes.
All accept sample retries, coverage and both concurrency defaults; `/2` adds
`defaults.max_failed_turns` and `defaults.call_retries`; `/3` adds
`defaults.max_consecutive_unmeasured`. Earlier majors reject keys introduced
later. The `/3` value is N, `"off"` or `null` (= fallback 3).

For a campaign Bundle, `selection`, `repeat_seeds`, `evaluation_set_id`,
`max_turns`, `timeout_seconds` and `metric_plan` are still explicit. Failure
settings are optional and resolve as above; they do not inherit the manifest.
The resolved plan is visible in `PreparedSummary.evaluation`; default threshold
3 is omitted from its canonical payload as described below. The applied turn
budget (`max_failed_turns`, `call_retries`) is echoed in `lab-evaluation-outcome/2`.

Call retries apply only to a rollout's own turn calls, never to `materialize`/
`release`, which the worker calls directly outside the retried call journal.
A typed retryable transient error can resend a turn only with
`turn_max_attempts > 1` and the target resending the same `request_id`. After
exhausted call retries, an `indeterminate` sample can use `sample_retries`.
Global stops still include cancellation, exhausted budget, a Core `exclude`
outcome (`cancelled` or `infrastructure_failed`) and a Core error without a
closed attempt. Reaching N consecutive unmeasured samples adds
`consecutive_unmeasured`; `"off"` disables only this stop. A scored sample resets
the consecutive count. Coverage must still reach the plan's `min_coverage`.
Keep coverage “N of M” and the `unmeasured` list beside any partial result.

### Saved plans and compatibility

Stored plans keep their original semantics and bytes: current omission defaults
are not applied to historical absent/null retry fields. Pre-dev60 plans retain
coverage threshold 1.0, no sample retries and scorer concurrency 1. The effective
`max_consecutive_unmeasured` is 3 for old plans and current default plans; the
canonical prepared payload omits that value to preserve the old digest. N other
than 3 or `"off"` is stored and changes the prepared digest. On the **same stored
Bundle**, an all-explicit old request keeps its plan bytes/digest. Newly captured
Bundles include the producing Lab environment and may have different digests.
There is no cross-pair migration: old runs stay with their producing wheels/state.

On exact Core b22 the instance lock serializes `turn` calls across sessions.
Overlapping other sample work and scoring does not guarantee a baseline speedup.

### Which split a fixed evaluation measures

A manifest `/15` corpus may label every row `split: train` or `split: test` (all rows or none). The prepare
request `lab-service-prepare-request/3` has a `split` field: `train`, `test` or `null` (= `train`; an unlabelled
corpus is all train). The chosen split is frozen into `case_ids` at preparation. An evaluation Bundle has no split, only `null`. Core checks the
labels (`partial_split_labels`, `group_crosses_splits`, `invalid_split`); Lab shows its code as is.

### HTTP preparation settings (dev66)

Generic `POST /v1/prepared-executions` accepts `lab-service-prepare-request/5`:
`/4` plus nullable `max_consecutive_unmeasured` (positive integer or `"off"`).
The closed raw `/5` evaluation body requires every key, including the new key;
`null` resolves the input/Lab default. `/4` has `sample_retries`, `min_coverage`,
`concurrency`, `scorer_concurrency`; `/3` adds `split` to `/2`. `/2`, `/3` and `/4`
remain accepted with their original key sets; new preparation uses the current
omission defaults. Campaign requests reject evaluation-only keys.
CLI files omit `schema`: naming `max_consecutive_unmeasured`, even null, selects
`/5`; naming only a `/4` setting selects `/4`; split alone keeps `/3`, otherwise
`/2`. The CLI fills the selected form's missing nullable keys. Project wrappers
retain `/1` and `/2`; generic `lab prepare` exposes the new settings.

### Unanswered samples

For each sample, a scorer that distinguishes no answer from an incorrect one may emit the normal
verdict `{"criterion": "answered", "passed": false}` for no answer and `passed: true` for an
answer. Lab counts false verdicts among admitted samples. `present` covers all admitted samples;
`partial` reports the count with `answered_reported`; `not_reported` and `unavailable` carry a
null count. The verdict is visible to the Reflector and does not change Core coverage or outcome.
