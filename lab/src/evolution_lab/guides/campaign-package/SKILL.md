---
name: evolution-lab-campaign-package
description: Assemble a Lab package - campaign manifest /15 or /16, or evaluation-only input /1 - and follow it to a prepared execution.
---

# Campaign package

This guide describes the documents a builder writes for a Lab package and the
path from those documents to a running operation. It covers both paths from
the [requirements catalogue](../requirements/SKILL.md#paths): `campaign`
(manifest `/15` or `/16`) and `evaluation` (`lab-evaluation-input/1`).

**The latest released pair is Lab `0.0.3.dev80` / exact core `2.0.0b21`.**
This guide describes development source, including schemas newer than that release.
See `docs/RELEASES.md` for delivery history and source provenance. Preserve the
delivered Core wheel matching `SHA256SUMS`.

Before a paid campaign, inspect the fixed baseline's `lab measurements` or
`lab report` for `scorer_without_focus_signal` and `lab_notices_state`.
The notice says that all `admitted` samples in that evaluation had empty
verdicts and `critical_pass: null`; `not_completed` counts execution outcomes
other than `completed`. Metrics alone do not build campaign focus. Execution
failures can still build it, so the notice does not predict all future rounds
will be clean. `lab_notices_state: unavailable` with an empty notice list means
the scorer evidence could not all be read, not that a focus signal was found.
Preparation and `check` do not run the scorer. See the
[scorer protocol](../evaluation-inputs/SKILL.md#scorer-protocol).

## Choose a project campaign's first baseline

For a baseline from a project directory, follow [first evaluation](../first-evaluation/SKILL.md). If the directory has `manifest.json` and `campaign-launch.json`, the first campaign uses `just campaign DIR` from the Lab checkout or `lab campaign DIR --wheel LAB_WHEEL --wheel CORE_WHEEL` from the installed CLI. Review the campaign inputs and [budget](../budget/SKILL.md) with the person before a paid run.

With Core's baseline import support, `lab campaign DIR` on `/16` selects the latest eligible fixed evaluation of the project, including earlier pair states, when its scorer traces are readable. Core measures missing pairs. Choose `--baseline fresh` for a full measurement or `--baseline-from RUN` to select one evaluation by run number or operation ID. On `/15`, import requires `--baseline-from` and verdict-only failures stay undiagnosed until the first baseline refresh (#208). Keep `baseline_import` out of Bundle manifests. Core alone decides whether a source needs an explicit acceptance.

For `seed_mode: exact`, use `[random_seed + 1, …, random_seed + R]` as evaluation `repeat_seeds`, with `R` equal to the campaign's `repeats_per_question`. Read `seed_differs` in the import summary when seeds do not match.

Lab accepts `evolution-v2-manifest/15` and `evolution-v2-manifest/16`
(core families 14 and 15).
Core's `evo-target init` writes `/15`, and Lab's own scaffold also writes `/15` with its observe smoke profile; `/16` is written
by the builder. Admission is on the Owner paths only: Bundle and preparation, Owner, service, CLI and the project form.
The H1 client, `recipe.prepare_inputs` and the legacy runner stay `/15`-only. Every other campaign schema, including `/11`
and `/14`, is refused before side effects: Bundle/Owner/service preparation uses `unsupported_schema` (stage `bundle`,
HTTP 422), legacy `recipe.prepare_inputs` raises `ValueError` listing the accepted schema, and H1 `connect()` uses
`manifest_invalid`. A `/15` or `/16` that core refuses (for example without `campaign.gate.mode`) is `manifest_invalid`
whose message carries core's key path and its `expected …` phrase. Catalogue item ids stay the same.

The exact Core archive SHA-256 is
`578f26a0c9c27f63a56fb049f90e4eb51c96e7180c429e333b25255247e0f6aa`.
Record the exact Lab wheel hash too; bundled guide edits change its bytes.

<a id="manifest-15"></a>
## Manifest `/15`

The document contains the seed, target, editable scope, campaign, environment, scorer,
Editor, Reflector, top-level `grouper`, top-level `role_visibility` and corpus path.
The campaign declares `evaluation.repeats_per_question` and a gate with `mode`, `stability`,
`focus.margin`, `causal`, `provenance`, `fresh_parent` and `global`, plus `round_stop` and
`baseline_refresh_after_rounds`. In `statistical` mode the focus and causal checks also
require `confidence`; in `observe` they must omit it and `global` must be `off`.
The scorer declares `metric_contract`. Core checks these keys before start; Lab passes
its refusal code and text. Sections for each: [stability and stop](#stability-and-stop),
[metric contract](#metric-contract), [Grouper](#grouper), [role visibility](#role-visibility).

The corpus may label every row `split: train|test` (all rows or none; a group never crosses
splits; the campaign needs a non-empty train). Grouping, reflection, the Editor,
measurements and promotion see only train. Lab's fixed evaluation measures the split
its request names (`split` in `lab-service-prepare-request/3`, `train` by default).
Core checks the labels and Lab shows core's code (`partial_split_labels`,
`group_crosses_splits`, `empty_split`, `invalid_split`).

**From the core's scaffold.** Generate the neutral directory with `evo-target init`,
then use `evolution_lab._scaffold_manifest.as_manifest_15` to set Lab's observe smoke profile:
`evaluation.repeats_per_question: 1`, `gate.mode: "observe"`, `gate.focus: {margin: 0}`,
`gate.causal: {tolerance: "measured_noise"}`, `gate.fresh_parent: true` and `gate.global: "off"`.
It keeps the scaffold's `stability`, `provenance`, `round_stop`, `baseline_refresh_after_rounds`,
`grouper`, `metric_contract` and `role_visibility`. Run it with the Python where Lab is installed:

```sh
<lab-python> -c 'import json,sys; from evolution_lab._scaffold_manifest import as_manifest_15; p=sys.argv[1]; json.dump(as_manifest_15(json.load(open(p))), open(p,"w"), indent=2)' <path-to>/manifest.json
```

The helper refuses anything other than the exact pinned scaffold. Apply it before adapting
values to your agent. `statistical` is never set by Lab: the manifest's author enables it
(for K5-level campaigns see `docs/notes/track-e-statistical-campaign.md`).

`gate.fresh_parent: true` (Lab's smoke profile) measures each Attempt's Parent beside its
Candidate in the same round, so a drifting target is compared fairly; it doubles the
per-Attempt measurements. Set `false` when the target is stable and cost or capacity
matter; ask core for the resulting [target capacity](#target-capacity).

<a id="manifest-16"></a>
## Manifest `/16` (family 15): independent acceptance {req:campaign.independent_acceptance}

`/16` is opt-in; `/15` keeps its meaning. Choose it when the number of repeats R must not depend on the rule that judges a
candidate: the same R is spent whatever the rule, and each scope is judged by its own rule. Lab only passes the manifest to
core; it does not decide the gate (see PDR-0015 and PDR-0017, amendments of 2026-10-19).

- **`gate.mode`**: `point`, `confidence` or `unfiltered`. `point` judges the observed gain against a threshold. `confidence`
  judges a one-sided lower confidence bound L at confidence c against the threshold; core records no two-sided interval.
  `unfiltered` applies no threshold (the candidate is not filtered numerically; it is not evidence of quality).
- **`campaign.gate.stages`**: optional `single` or `focus_first`. Absent or `single` keeps the existing single-stage
  behavior. `focus_first` measures every focus case at all R repeats first and decides focus once; only a passing
  focus decision opens the causal stage, when causal cases exist. A nonpassing or unavailable focus comparison
  leaves causal cases unmeasured; an unavailable comparison is not evidence of poor quality.
  Core recommends `focus_first` in its [complete `/16` example and focus-first contract](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#focus-first-attempts).
  Keep `single` when comparability with a `single` campaign matters, including seeded `measured_noise` and
  `statistical` results: policy and binding digests differ, so seeded quantities can differ too.
  Adding, removing or changing this key on an unfinished run changes policy; authorize `rebaseline` or `close`
  under [Core continuation rules](https://github.com/sb-ai-lab/evolution/blob/main/docs/running-campaigns.md).
  Panel, CLI and Lab report text show the same Owner stage states for focus and causal: `waiting`, `open`, `passed`,
  `rejected`, `unavailable`, `not_run`, `incomplete` or `unknown` (the panel labels `not_run` as `not run`
  and `unknown` as `—`). Lab restates Core's stage facts and computes no gate verdict. Human-facing stage
  summaries show no slot savings; Core's slot counts remain in JSON. Lab's demo and scaffold stay `/15`.
- **`gate.execution_noise`**: a tagged object `{kind: negligible | bounded | observed}`; `bounded` also carries an
  `allowance` (a number ≥ 0, in primary-score units) and a public `source` text. `negligible` is core's recommendation.
- **`gate.verification`**: `reuse` or `fresh`. It changes only the Candidate side of the final check; the reference is
  always the round's champion.
- **`gate.fresh_parent`**: with `true` the focus and causal checks compare against a fresh re-measurement of the round's
  champion, with `false` against the round's stored baseline. In family 15 the Parent is always the round's champion, so
  there is no separate "Parent" reference.
- **`evaluation.repeats_per_question`** (R) is chosen independently of the mode. `confidence` also requires the
  `statistics` object. Without an execution policy, `provider_only` is passed through to Core unchanged;
  roles routed under a policy must put provider pins in that policy instead.
- **R = 1 and `confidence`.** Even a perfect 0 to 1 gain needs at least 4 focus units to pass at confidence 0.9, and 5 at
  0.95 (computed with core's `passes_bound`). With few units many attempts close as `comparison_preflight_failed` with
  no numbers; the campaign view carries core's attainability facts (preflight ceilings, local guidance) for this.
- **Promotion.** In `/16` promotion is automatic. Core b8 removed manual promotion: a `campaign.promotion` key is refused by core as
  `unknown_key`, and Lab has no promotion decision action. `linked_from` is refused as `linked_run_unsupported`.
- Lab never writes `/16` for you: `as_manifest_15` and the scaffold stay `/15`.

Core b17 adds optional `/16` settings, passed through to Core unchanged:

- `require_parameters` on chat roles defaults to `true`; declaring it requires
  `provider_only` or `provider_order`. Setting it to `false` may allow a provider
  to ignore unsupported parameters. It does not apply behind the Lab gateway:
  roles under an execution policy cannot pin providers in the manifest.
- `nearest_paths` on chat Editor and Reflector defaults to 5 and controls the
  number of nearby readable paths suggested for a missing path.
- `campaign.reflector_context` supplies context to the Reflector;
  `campaign.context_max_bytes` sets the context byte limit (default 65,536).

Core b18 adds two more optional `/16` settings, also passed through unchanged; Core validates them:

- `reflector.selection.min_claim_cases` (default 2) is the smallest distinct claim across hypotheses that sets the
  gate's focus; a smaller claim uses the selected portion's cases (gate source `below-minimum`). Changing it on resume
  takes `rebase`. See [Core grouping and selection](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#grouping-and-selection).
- `campaign.evaluation.focus_repeats_per_question` measures each Attempt's focus cases (Candidate and fresh Parent)
  more often than `repeats_per_question`; baselines, completion, verification and Champion stay at R. Core admits it
  only with `gate.mode: point` and `gate.fresh_parent: true` and a value greater than R, otherwise it refuses the
  manifest before a run. Changing it on resume is a policy change (`rebaseline` or `close`). See
  [Core focus repeats](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#focus-repeats).

Core b13 adds these `/16` settings; Core validates them and owns their rules:

- `evidence` controls the role evidence volume; see [Core evidence](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#evidence-manifest-16).
- `checks` selects policy-check modes; see [Core check modes](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#checks-manifest-16).
- Role-loop limits bound repairs, tool failures, grouping fallbacks and skipped rounds; see [Core role-loop limits](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#role-loop-limits-manifest-16).
- `campaign.indeterminate.<owner>.*` controls each owner's indeterminate policy; see [Core campaign settings](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#campaign).
- `target.preflight_sample_retries` controls connection preflight sample retries; see [Core target settings](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#schema-seed_root-target-and-artifacts).
- `reflector.probe.string_body` controls JSON-string probe bodies; see [Core live probes](https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#optional-live-probes).

Core's b13 delivery reports that `qwen/qwen3.8-flash` sends the probe body as a
JSON string even with the typed schema; use `string_body: "parse"` in
`reflector.probe` for that model.

Lab shows non-`enforce` check modes and Core's shadow hit counts in the campaign
view, CLI, markdown report and panel, even when independent acceptance data is
unavailable. Start and resume logs explain each non-`enforce` mode's consequence.
Counts from different checks are not comparable; do not add them into a total.

Finish `/16` campaigns started on dev60/b12 with their producing pair and state.
Lab does not migrate state between version pairs; use the producing wheels and state.

The view retains both nullable `gate_comparison` and `global_comparison`, each with its own scopes and nullable
`reference_measurement_id`. Core's `comparison` is global when present, otherwise gate; Lab copies all three independently.
CLI, markdown and panel show both recorded levels, omitting absent levels. Known limits: only a one-sided lower bound
is shown. Promotion is automatic and L3 is unavailable. See [diagnostics](../diagnostics/SKILL.md) for reading results and [campaign operations](../campaign-operations/SKILL.md#propose-continuation-or-a-successor) for linked starts.

<a id="confidence-gate"></a>
### The confidence gate: `observe` or `statistical` {req:campaign.gate}

This section describes `/15` (family 14); `/16` is [above](#manifest-16). Core defines passing the gate, noise calibration
and repeats without variation; Lab carries its decisions (below).

- **`observe` (Lab's default, the smoke profile).** One repeat of each question, no intervals; core filters nothing
  numerically, and the result permanently carries `observe_gate_not_enforced` and `uncalibrated_point_estimates`.
  Cheap; it checks the wiring, not quality (core: `observe` can promote a worse Candidate). Lab marks it everywhere:
  the report says «Гейт не применялся (observe)», `lab campaign` prints `gate: observe — not evidence of quality`, the
  panel shows a neutral mark.
- **`statistical`.** Set explicitly: `gate.mode: "statistical"`, `evaluation.repeats_per_question` R ≥ 2 (every
  measurement costs R times more), `gate.focus.confidence` and `gate.causal.confidence` (confidence levels),
  `gate.causal.tolerance` (`measured_noise` or a number), `gate.global` (`off`, `point`, `bound`); `/15` also requires
  `gate.focus.margin` in both modes ([below](#stability-and-stop)). How much this costs is the person's decision; Lab
  does not estimate it.
- Before choosing the numbers, know what the gate can see. Cases are not resampled, only the repeats inside a case:
  0.90 means "an effect larger than the repeat noise on these cases", not a promise about new questions — measure
  quality on `test`. `causal.tolerance: 0` rejects whenever the lower bound on the other cases is below zero, like
  `regression_margin: "0"` on `/11` (there, with one look and one repeat, promotion is nearly out of reach). A
  `causal_below_tolerance` attempt is shown under "Not better": a regression was not ruled out, not shown.
- Lab does not check the gate keys itself: core does, before the start (Bundle, H1 `connect`), and Lab passes its
  code and text.

**On `/15` (family 14).**

- A Candidate that passes the gate is a *finalist*, not an accepted one: its Attempt closes `finalist` with the reason
  `gate_passed` (`statistical`) or `observe_finalist` (`observe`; a critical failure still rejects it). Acceptance is
  decided afterwards by core's final contrast: a fresh measurement of the finalist over the whole corpus, compared
  with the Champion under `gate.global` and the guards (a critical failure, an incomplete final). Lab takes that
  verdict from core and shows it as the Attempt's `final_decision` (`accepted`, `rejected` with the contrast's reason,
  `pending` while the round's final is not yet measured, `unavailable` when the final did not take place); it
  recomputes nothing.
- Repeats without variation. When the repeats show no variation inside any retained case, family 14 decides the
  comparison on its observed point instead of closing the Attempt `insufficient_evidence`: the focus passes when the
  point is above `gate.focus.margin`, the causal check when the point is at least minus the tolerance. The estimate
  carries `exact: true` and the caveat `exact_contrast`, which says only that no within-case variation was observed in
  R repeats, not that the agent is deterministic.
- `measured_noise`: a Parent with no observed variation gives a measured tolerance of exactly 0 and the caveat
  `noise_not_observed`; it does not close the Attempt, and a Candidate that varies must still pass the causal check
  against that 0. `measured_noise` needs at least two Parent observations per causal case; without them a
  `statistical` Attempt closes `insufficient_calibration`.

**Current reader and compatibility.** The development source with exact core 2.0.0b22
reads family 14 and family 15. `lab-campaign-view/15` carries `family: 14 | 15`. For family 14 it keeps the `/7`
content: it omits `rounds[].cohort` and `rounds[].attempts[].alpha_total`, including null keys; `look`,
`confidence.margin`, estimate `exact`, `final` and `final_decision` remain. Family 15 carries an `independent` block
instead. Owner report is `lab-owner-report/15` (includes `lab_independent`); status/list use `lab-operation-status/8` and `lab-operation-list/8`.

For family 15, view `/15` also carries `checks`, `checks_state` and
`checks_reason`; the nested Owner report `/15` carries `lab_checks`,
`lab_checks_state` and `lab_checks_reason`. An admitted projection has state
`present` and null reason: checks is null if Core has neither map, otherwise
`{modes: {...}, shadow_hits: {...}}` with an absent map copied as `{}`.
An unreadable projection has null checks, state `unavailable`, reason `unreadable`.
These fields remain independent of binding-read failures and `independent`.
Family 14 has none of these keys. Modes and individual counts are copied from
Core; counts of different checks are not comparable and must not be summed.

An empty or torn creation without validated identity is unavailable, not a campaign
of another family. A confirmed foreign family is refused; a wrong exact core pin
is a separate refusal. H1 status treats a wrong pin as unknown (`lab_error`),
while its report has reason `unknown`. Unknown never means success.
Old runs, resume and cleanup stay with their producing wheels and state. Choose a
fresh private state path for the producing pair; no directory is created by this guide.
Owner schema13 upgrades a schema11 or schema12 state in place, after which an older Lab refuses it;
earlier Owner schemas, including v9, are refused without migration.
A matching schema does not make another pair's state reusable. Do not copy old folders.
The panel shows every recorded final entry in core order, settings, roles,
connection and baseline refreshes, plus confidence `margin` and estimate `exact`.
Final entries include members, contrasts, selection, `listed` and next baseline.
Finalist, final decision and Champion transition are separate facts: only present
`champions[]` lineage with `origin: promoted_attempt` and the matching attempt ID
confirms promotion. A recorded transition does not assert the current Champion.

`Owner.suspensions`, GET `/v1/operations/{id}/suspensions` and
`lab suspensions OPERATION [--json]` share the closed `lab-suspension-evidence/1`
read. Its identifiers and codes explain recorded pauses, not all remaining work;
counts, digests and truncation remain explicit. The panel reads current evidence
on the suspended screen and loads history after resume when opened. New sequence
values invalidate old answers; failures mark retained evidence stale, and result
deletion clears it. Unavailable or stale history neither proves there were no
pauses nor confirms a current cause.

Owner SQLite schema13 records each start's prepared digest; schema12 added each start's Core refusal findings,
schema11 durable start initiation metadata and schema10 execution-policy confirmation and per-start policy history. It retains reflector-probe scopes and Core-terminal facts. Connect
upgrades a schema11 or schema12 state in place, after which an older Lab refuses it; earlier Owner schemas are
refused with `unsupported_schema`, without migration.
Use a fresh private state only after the producing pair is accepted separately.
A matching schema does not make another producing pair's state reusable. This
guide does not create state, migrate consumers or authorize release.

**Scorer content on exact Core b22.**

Lab uses core's public `ScorerTraceReader` for directory and pack traces,
addressed by the selected measurement and scoring request. Completed journal
records still gate disclosure and determine scoring state; identity/hash checks,
`lab-sample/1`, campaign `/15`, families 14 and 15, private disclosure and key masking stay
unchanged. Missing records are `trace_missing`; unknown request sizes are
`trace_missing` for packs and conservatively `unreadable` for directories.
Verified input/dialogue survives missing stdout with `trace_missing` (pack) or
`unreadable` (directory), even for failed scoring. Empty failed stdout remains
available; admitted scoring still requires valid JSON. A failed payload read
preserves no unverified input. Existing private failed-scorer `exit_status` can
carry core's returncode; directory traces give null. No stderr is read.

One core `ScorerTraceReader(root, run_boundary="nofollow")` is opened per
request. Metadata (`sections=()`) and payload use that same reader; payload reads
pass `max_section_bytes=33554432` (32 MiB per selected section). Request/stdout
metadata and returned bytes are also checked before parsing/disclosure. Oversize
rejects the whole trace as `too_large`, without partial input. Typed `too_large`
maps to that code; other typed read failures map to `unreadable`.

Strict no-follow traversal and bounded payload reads replace the prior b5
root-swap/file-growth exceptions. The section limit does not cap the whole pack
index or process RSS and does not make files immutable to their owner. This
residual limit is explicitly accepted for exact dev54/b6 (separately, as for local dev53/b6 before it); no successor, including a
Lab-only version on b6, inherits acceptance automatically. For exact dev56/b8 the user accepted it separately on 2026-10-20 (not inherited). For exact dev57/b9 the user separately accepted it on 2026-10-20; dev56 acceptance did not transfer. For exact dev58/b10 the user separately accepted it on 2026-10-22; dev57 acceptance did not transfer. For exact dev59/b11 the user separately accepted it on 2026-10-23; dev58 acceptance did not transfer. The Core reader modules are unchanged: nofollow and 32 MiB per selected section remain, without an aggregate pack-index/RSS cap or owner-file immutability. For exact dev60/b12 the user separately accepted this same residual limit on
2026-10-25; b11 acceptance did not transfer. Every next pair needs its own assessment. Old pairs/runs remain
with their producing environments; there is no automatic shutdown or migration.
Other raw readers retain descriptor-relative no-follow reading.

For exact dev62/b14, the user separately accepted this residual on 2026-10-27
under ADR-0019, including the audited additive root exports and writer's
process-local Lock-to-RLock change. Reader implementations and pack format
remain unchanged; nofollow and 32 MiB per selected request/stdout section remain,
without an aggregate pack-index/RSS cap or owner-file immutability. Dev61/b13
acceptance did not transfer. Every next pair needs its own assessment and
explicit acceptance. This does not authorize release, migration or state creation.

<a id="stability-and-stop"></a>
### Champion stability, round stop and baseline refresh {req:campaign.stability_and_stop}

- `campaign.gate.stability` decides whether core re-measures the Champion in every final; `unstable` does, and costs
  more. Lab's smoke profile keeps the scaffold's value.
- `campaign.round_stop` stops attempts at the first accepted final decision or runs all Attempts (scaffold: `first_accepted`).
- `campaign.baseline_refresh_after_rounds` sets how often the baseline is re-measured (scaffold: 3).
- `campaign.gate.focus.margin` is the effect threshold; core requires it in both gate modes (the smoke profile writes `0`).
- All four are the person's decision about money and time. Lab does not check their meaning.

<a id="metric-contract"></a>
### What the agent must return: metric_contract {req:eval.metric_contract}

- `scorer.metric_contract` names the fields of the target's reply (`target_fields`) the scorer cannot score without;
  the scaffold's value is `assistant_message /content nonempty_string`.
- Core checks the connection before the first rollout: one Champion rollout on the first train case through the target
  and the scorer. It costs exactly one case and does not check the role routes (Reflector, Editor and Grouper are not called).
- Outcome: `compatible`, or a pause. For `connection_incompatible` and `connection_preflight_inconclusive`, repair causes outside the Bundle before resuming it. For an error in the Bundle (agent, scorer, `metric_contract` or campaign budget), prepare corrected inputs and follow [campaign operations](../campaign-operations/SKILL.md#propose-continuation-or-a-successor) for admission or a new start.
  `connection_preflight_unavailable`: target or scorer unreachable - fix it and resume.
- Changing `metric_contract` on resume is refused by core as `run_identity_changed`.

<a id="grouper"></a>
### Grouper {req:campaign.grouper}

- The top-level `grouper` splits the round's failures into groups for the Reflector and replaces `reflector.grouping`.
- Kinds: `fields` (by reply fields, free; the scaffold's kind), `command` (own program) and `chat` (a model).
- Secrets: `command.allowed_env` and the `chat` API key (and proxy, where core declares one) join the run's secrets
  and are redacted; paths (`command.cwd`, the program, a declared `system_prompt`) are relocated with the Bundle.
- `agent_cli` is not supported by Lab yet: `grouper_kind_unsupported` (at `grouper.kind`, on every preparation path,
  before any side effect);
  support is Lab's next task.

<a id="role-visibility"></a>
### What roles may read {req:campaign.role_visibility}

- The top-level `role_visibility` lists the agent-tree paths the Editor and the Reflector may read (scaffold: `[]`).
- It is visibility, not a sandbox.
- Changing it on resume is refused by core; the only way out is to close the campaign. Lab passes core's refusal as is.

### Manifest sections

| Pointer | Content |
|---|---|
| `/seed_root` | Directory of the seed tree that the campaign evolves. |
| `/target` | `base_url` and `seed_mode` (required); optional `contract_digest`, `settings_hash`, `max_response_bytes`, `min_input_bytes`, `timeouts`. Must agree with the launch file; see the [target connector guide](../target-connector/SKILL.md#launch-file). |
| `/artifacts` | `writable_paths`, `dependency_paths`, `excludes` (below). |
| `/role_visibility` | `{version: "role_visibility/1", readable_paths: [...]}`; see [role visibility](#role-visibility). |
| `/campaign` | Scale, gate, evaluation, stop, refresh and metric settings. |
| `/environment` | `static` or `scripted`; see the [evaluation inputs guide](../evaluation-inputs/SKILL.md#corpus). |
| `/scorer` | Scorer command and `metric_contract`; see [Scorer protocol](../evaluation-inputs/SKILL.md#scorer-protocol). |
| `/editor` | The role that proposes changes. |
| `/grouper` | `fields`, `command` or `chat`; see [Grouper](#grouper). |
| `/reflector` | The role that analyses measurements for the editor. |
| `/corpus/path` | Corpus file outside `seed_root`, `scorer.cwd` and every path a role or Grouper declares (`cwd`, `system_prompt`, `agent_bin`, `environment_docs`), or preparation refuses `corpus_inside_role_workspace`. |

A manifest with `linked_from` is refused as `linked_run_unsupported` on every Lab path.

<a id="suspension"></a>
## Suspension

Use [campaign operations](../campaign-operations/SKILL.md) for the decision between resuming with saved inputs, selecting a new preparation, and starting a linked successor.

An infrastructure failure that used to close a campaign as
`infrastructure_failed` now leaves it **suspended** instead: core records a
non-terminal `campaign_suspended {reason, owner, evidence}` entry and stops
advancing the run, but the campaign is not finished. `CampaignResult.terminal`
is `"suspended"`, `result` is `None`, and `campaign_outcome` stays `null` — the
outcome is *unknown*, not a failure.

You see it as:

- **Owner status.** `control_state: "finished"`, `process.exit_code: 1`,
  `result.core.suspended: true`, and `result.core.suspensions[]` (each entry
  has `seq`, `reason`, `owner`, `evidence_kind`).
- **`lab status` (human output).** Two extra lines:

  ```text
  suspended: <reason> (owner=<owner>, evidence=<evidence_kind>)
  continuation: lab resume <id> --target <ready target of the same bundle> --generation <owner generation>
  ```

  The line follows the status field `resume`. A `cleanup_required`/`orphaned`
  operation, or a `released` one whose cleanup fact was lost, prints
  `continuation: lab cleanup operation <id>, then lab resume …` instead; a
  `released` operation after a manual `lab release-operation` prints `continuation: not
  available after manual release; the run directory is kept until results are
  deleted`. A client reads the same answer from `resume` in
  `lab-operation-status/8` (`ready`, `cleanup_first`, `busy`, `refused` + code,
  or `null`). The verdict has a third key, `target`: `null`, or — after a
  `target_unstable` suspension — `{requirement: "fresh_instance", reason:
  "target_unstable_fresh_required", excluded: [target refs of every start of
  the campaign]}`. Continue on a target that is not in `excluded`; `lab resume`
  on an excluded one is refused with `target_unstable_fresh_required`. With
  such a requirement the `continuation:` line reads `on a fresh target: stop
  target <id> if it is still running (lab agent stop … --wait), start a new target of the same bundle (lab agent start <connection>),
  then lab resume …`.

- **Role suspensions (family 10).** Four more reasons name a failing role:
  `reflector_provider_failed`, `reflector_failing`, `editor_provider_failed`
  and `editor_failing`. `lab status` prints a `hint:` line after `suspended:`.
  - `*_provider_failed`: the role's provider failed or refused; repair the
    provider's availability, key and quota.
  - `editor_failing`: the Editor failed outside its edit (a timeout, a crash,
    an agent or program error, a failed preflight); repair the external cause
    (network, host resources, an agent CLI's login).
  - `reflector_failing` has two triggers: the same kind of failure of the
    Reflector outside its diagnosis, or two error groups in a row skipped, each
    after `reflector.max_consecutive_failures` content failures. After an
    external or transient failure, repair it and continue. Repeated content
    skips may have no external cause: continuing helps only if the failures
    were transient. Changes to the Reflector's program, prompt, limits or
    `max_consecutive_failures` require newly prepared inputs and Core admission.
  - A content failure is not a suspension: for the Reflector it closes the
    round `unplanned` and counts toward that group's skip; for the Editor it
    closes the Attempt (`abandoned` / `editor_failed`).

  With saved inputs, repair causes outside the Bundle. The
  interpreter, installed tools and runtime files are part of the Bundle's
  captured execution environment, which Lab re-captures on every start
  (`environment_invalid` on a change). Changing a role's program, prompt or
  limits (including `max_consecutive_failures`) or that environment needs a new
  preparation and Core admission. After a
  role suspension `lab resume` needs no particular target: the status field
  `resume` is `ready` with `target: null`, and a fresh target is required only
  after `target_unstable`.

- **Report.** A `suspensions` table (`seq`, `reason`, `owner`,
  `evidence_kind`), the indeterminate window, and, per measurement,
  `call_failures_by_code` and `call_retries`; see "Campaign reports" in Read
  the results (in the Evolution repository: `docs/guide/results.md`).

**Goal:** decide what to do with a suspended run.

1. **Do:** check whether it is a legacy-runner run
   (`<run>-lab-logs/supervisor.lock` exists). If so, fix whatever made the
   target's calls fail, then call `runner.resume_native` in the same run
   folder. It continues the suspended campaign because
   `runner._campaign_state` only refuses a *finished* (`complete: true`)
   campaign, and a suspended one is not finished — no code change, no new
   authorization, no new baseline.
2. **Do:** for an Owner, service or CLI run, fix the target (or start a new
   `ready` target of the same Bundle), then continue explicitly: `Owner.resume(
   ResourceRef('operation', id), target_ref=..., secrets=...)` /
   `lab resume <id> --target <id> --generation <gen>`. This works on any
   `ready` target of the same Bundle, including after an Owner/service
   restart, once the operation itself is `finished` or `released` with
   confirmed target cleanup. A `cleanup_required`/`orphaned` operation needs
   `lab cleanup operation <id>` first; a `released` operation after a manual
   `lab release-operation` has no continuation, only [deletion](../owner/README.md).
   H1 runs still have no continuation path in this Lab version.
3. **Done when:** either the legacy run reaches `campaign_finished`, the
   Owner/service/CLI run reaches `complete` on a later start, or you have
   accepted that the run (H1, or a manually released Owner/service/CLI run)
   stops here.

<a id="editable-scope"></a>
## Editable scope

`/artifacts` decides which files of the seed tree the campaign may change and
which ones it only depends on. Patterns are relative to the seed root and
gitignore-like: a pattern without `/` matches a name at any depth; a pattern
with `/` is anchored at the root, and `**` matches zero or more segments.

| Key | Meaning |
|---|---|
| `writable_paths` | Patterns the editor may change. Everything else is fixed for the candidate. |
| `dependency_paths` | Plain file paths (no wildcards, not excluded) that the candidate depends on without changing, for example a lock file. |
| `excludes` | Patterns left out of the candidate. Core adds its default excludes (version-control directories, virtual environments, caches, `.env` files, logs). |

A writable pattern may not match a dependency path or an exclusion. For the
synthetic order-lookup assistant, `writable_paths: ["skills/**"]` lets the
campaign rewrite skill files, `dependency_paths: ["uv.lock"]` pins the
environment, and the tool code stays outside the writable set.

Items: {req:scope.editable}, {req:scope.invariants}, {req:agent.skills_layout}.

<a id="editor-and-reflector"></a>
## Editor and reflector

Custom subprocess Reflectors must accept `reflect_brief/5` for `/15`
and `reflect_brief/6` for `/16`. Editors receive `edit_brief/4` for `/15`
(or `edit_brief/5` with sequential attempt feedback), and `edit_brief/6`
for `/16` (or `edit_brief/7` with that feedback).
Keep older role programs and runs in their producing environments; do not
upgrade an old run in place.

`campaign.editor_context` is shared context: Core shows it to both the Editor
and the Reflector, so it must not contain instructions meant for one role only.
Put role-specific instructions in that role's prompt or program.

Both roles use one of three kinds:

- `subprocess` — a command section like the scorer's (`argv`, `cwd`,
  `dependency_paths`, `allowed_env`). The program receives the Editor or
  Reflector brief for its manifest version, as listed above. Sequential
  `campaign.attempt_feedback` adds earlier attempts' feedback to the Editor brief.
- `chat` — requires `model`, `base_url` and `retry`
  (`role_call_retry/1`); `api_key_env` is optional. If absent, core sends no
  authorization headers; a declared but empty variable is refused. Optional keys include `system_prompt`,
  `reasoning_effort` and size limits.

  On exact b22, `base_url` may use `http` to any host. Core's public endpoint
  helpers emit a WARNING for non-loopback HTTP: the credential is sent unencrypted
  when `api_key_env` is declared, otherwise the traffic is unencrypted. Loopback
  HTTP does not get that WARNING. Every role without `api_key_env` gets the
  separate "without credentials" line, including loopback roles. Lab preserves
  these lines in the campaign worker's stderr log; prefer HTTPS for remote hosts.
  Adding or removing the declared key on an existing campaign is one
  `run_identity_changed` (`rebase`) and one operator `evo tree authorize`, not a
  transparent Lab resume. The sealed Bundle cannot be edited for continuation;
  Lab does not perform that authorization or continue under a changed identity.

  On `/15`, set `context_compact_chars: 800000` and `context_message_max_chars: 64000` on chat roles explicitly
  when the model's window is about 400k tokens or more. With core's defaults (128 000 / 12 000) the roles fold
  their history, re-read the same evidence in a loop and run out of turns (on one measured stand, 0 of 4 editor
  attempts produced a candidate on the defaults, 3 of 3 with the raised limits). The cost is the window: up to
  ~8 million input tokens per editor attempt. For a smaller window convert it to characters with a margin.
- `agent_cli` — requires `model`; optional keys include `system_prompt`,
  `allowed_env`, `reasoning_effort` and size limits.

On `/15` every Reflector kind also requires `environment_docs`, `probe` (each may be
`null`), `selection`, `families` and `max_consecutive_failures` (integer 1–20, no default;
the scaffold writes 2). Grouping belongs to the top-level `grouper`.
`max_consecutive_failures` limits consecutive content failures on one group; skipping two
groups in a row suspends the campaign with `reflector_failing`.

A `chat` role's required `retry` has the `role_call_retry/1` form:

```json
{"version": "role_call_retry/1", "max_attempts": 3, "total_budget_seconds": 600,
 "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 30, "jitter": "full"}}
```

`max_attempts` is 1–10, `total_budget_seconds` is 1–3600; the backoff has exactly the four
keys shown. `subprocess`, `command` and `agent_cli` roles must omit `retry`. A chat role
without it is refused as `missing_key`. The author chooses these limits; Lab never fills
them in (PDR-0015). The retry budget also limits one role request: keep the gateway's route
`timeout_seconds` at least as long as the expected request, the role's retry budget at
least that long, and `reflection_timeout_seconds` / `editor_timeout_seconds` above it.
Guidance for the Editor goes into its `system_prompt` or subprocess program.
Role `base_url` values are part of the campaign identity; see the
[pitfalls guide](../pitfalls/SKILL.md).

<a id="reflector-probe"></a>
### Reflector probe launch (`lab-target-launch/3`)

This is the HTTP environment copy used by the campaign's Reflector, separate
from interactive target probe sessions. The manifest's `reflector.probe`
declares its API URL and health path; launch `/3` declares how to run it.
Lab does not verify that the probe and target serve the same data snapshot or
pass the current Champion to the probe. Arrange matching data in your integration.

For example, the manifest's `reflector.probe` object can be:

```json
{"base_url": "http://127.0.0.1:8781", "health_path": "/health", "request_timeout_seconds": 10}
```

Core validates the declaration; Lab reads the loaded manifest rather than
parsing new `/16` keys itself. At campaign Bundle creation the declarations must agree:

| Manifest `reflector.probe` | Launch `reflector_probe` | Result |
|---|---|---|
| Absent or `null` | `null` | Valid; no probe launch or health check |
| Declared | Object | Lab starts and cleans up the probe |
| Declared | `null` | Operator runs it by hand; Lab checks startup health once |
| Absent or `null` | Object | Refused with `manifest_invalid` |

`/1` and `/2` remain accepted; their absent launch field means no managed probe.
Evaluation-kind and interactive probe-kind Bundles refuse a non-null
`reflector_probe` with `invalid_request`. Custom command-form `/1` targets do
not get a managed reflector probe.

The campaign scaffold writes `/3` with `"reflector_probe": null` and
`"turn": {"empty_answer": "target_failed"}`. To launch a neutral HTTP
emulator-shaped service from your own package, replace the null with:

```json
{
  "environment_ref": "<Bundle environment digest>",
  "executable": "<that environment's interpreter>",
  "args": [
    {"bundle_path": "local/probe_server.py"},
    {"literal": "--port"}, {"probe_port": null},
    {"literal": "--data"}, {"bundle_path": "data/reference"}
  ],
  "startup_timeout_seconds": 60,
  "stop_grace_seconds": 5,
  "max_restarts": 3
}
```

All six fields are required; 60 seconds, 5 seconds and 3 restarts are guide
values, not runtime defaults. Timeouts must be positive finite numbers and
`max_restarts` an integer ≥ 0. `environment_ref` names a declared Bundle
environment and `executable` must match its interpreter. `args` accepts only
`literal`, `bundle_path` and `probe_port` tagged objects. `bundle_path` relocates
scripts/data into the Bundle; literals are passed unchanged. The working
directory is private Owner state, so a literal relative script/data path does
not resolve from the Bundle. The process receives the target launch's declared
`env` and supplied secrets.

`{"probe_port": null}` takes the port from `reflector.probe.base_url`, the
single source of truth and part of Core's campaign identity. It may occur
zero or more times; do not duplicate the port as a literal. An active listener
refuses start with `reflector_probe_port_busy`; TCP `TIME_WAIT` after cleanup
can be reused. The bind/listen check is not an atomic reservation: the actual
server bind remains authoritative, and a reusable bound socket that is not
listening may pass the check.

On start and explicit resume, after matched target health and before worker
dispatch, Owner starts the probe and waits for a 2xx response from
`GET <base_url><health_path>` within `startup_timeout_seconds`. Under `suspend`,
unavailable health refuses with
`reflector_probe_unavailable` before the campaign worker or model spend, and
Lab cleans up its probe. For an operator-run probe, Lab makes exactly one GET
using the manifest's `reflector.probe.request_timeout_seconds`; it neither
continuously monitors nor stops that external process. A manifest without a
probe needs no probe server.

On exact b22, `/15` and `/16`'s default or explicit `on_unavailable: suspend`
keep that refusal. `/16` with `on_unavailable: continue` starts even if health
is unavailable and prints `evo tree evolve: note: reflector probe at <base_url>
is unavailable at start; reflections continue without it`. A launched child
stays owned and watched so it can become healthy later. Core owns the
consequence of a probe outage during the campaign. The report preserves Core's
per-reflection probe lines from both finished and failed reflections after a
health check. Runs that declared `on_unavailable` stay with their producing pair: b13 cannot
read this declaration. This does not migrate existing runs or authorize continuation.

Each Lab launch/restart gets its own Owner helper scope and cleanup receipt.
If the child exits while the worker runs, Owner fully reaps it before trying
a replacement on the same port, at most `max_restarts` times per worker run.
Exhaustion stops replacement attempts; it does not make Lab resume the
campaign. Explicit resume starts a fresh probe with a fresh restart count.
When the worker finishes, suspends or fails, Owner sends STOP and waits for
helper cleanup; Owner-death cleanup uses the existing scope mechanism.

Launch, exit, restart and exhaustion notes go to private worker `stderr.log`;
they are not streamed as CLI events. CLI campaign/watch/report show aggregate
probe facts, and the panel shows all four fields beside checks. Current
`lab-campaign-view/15` and `lab-owner-report/15` have a top-level triplet:
`reflector_probe`, `reflector_probe_state` (`present` or `unavailable`) and
`reflector_probe_reason` (null or `unreadable`). A present record is exactly
`{launched_by, state, restarts, last_exit}`: attribution is `lab` or `operator`,
state is `running`, `stopped` or `exhausted`, restarts counts replacement
attempts and last_exit is a signed integer exit code or null. These are Owner
facts independent of Core/report availability. Only an explicit
`{launched_by: null, state: not_declared, restarts: 0, last_exit: null}` means
no declaration; missing or malformed evidence is unavailable, never inferred
absence. For an operator server, `running`/`stopped` records this start's
participation; `stopped` does not mean Lab killed it, and `last_exit` stays null.
Markdown retains its existing unavailable result when there is no Core report
to render.

### Budget gateway for model roles

Point each paid role at its gateway base URL, for example
`http://127.0.0.1:8790/editor/v1`, and give it only that route's client token
through its declared `api_key_env`. The gateway alone holds the upstream key.
Set the role's `max_output_tokens` no higher than the route's `max_tokens`.
For a Lab-managed gateway, use a confirmed
[execution policy](../budget/SKILL.md). Keep role `model` equal to the route's
`client_model` and keep the gateway URL in the Bundle. Provider pins belong
in the policy; `reasoning_effort` remains in the manifest. Manifest
`provider_only`, `provider_order` or explicit `allow_fallbacks` on routed chat
roles is refused as `manifest_provider_pin`, before calls. Without a policy,
Core provider-pin pass-through and the existing operator-run `Route` API
remain available.

For a suspended campaign, stop the target and wait for cleanup, confirm the
new reviewed policy with its expected digest, start a target of the same
Bundle under that digest, then resume explicitly. Keep the complete `gateway`
block identical; policy presence cannot change. Core identity and the
measurement stay the same and Core does not see the route change. The human
must establish model and quantization equivalence: Lab does not check it.
The [policy workflow](../budget/SKILL.md#change-a-suspended-campaigns-provider)
includes CLI commands, token bindings, audit and unavailable-evidence rules.

The gateway reserves at the declared tariff for
`min(context_tokens, request JSON bytes)` input plus the request's maximum
output. This uses its serialized body after provider/reasoning injection.
With valid nonnegative integer prompt/completion token counts, it settles
from finite nonnegative numeric `usage.cost` when the recognized protocol
currency matches the gateway (OpenRouter USD, RouterAI RUB); booleans are
invalid. Otherwise usable tokens settle at the declared tariff. Missing
tokens keep the reservation even if cost is present. Provider cost already
includes cache discounts.

`requests.settled_source` is `upstream_cost`, `tariff` or `refused`; old
settled NULL-source rows remain tariff totals. `/health` and `summary()`
split `settled_upstream_units` from `settled_tariff_units`, count recognized
protocol tariff fallbacks in `usage_cost_missing` (including currency
mismatch), and releases in `released_refusals`. Used money remains settled
amounts plus held reservations (`reserved_upper_units`). A settlement above
reservation but within the reported-token tariff is a `bound_observations`
entry. Above `max(reserved, tariff_actual)` it stops the gateway as an overrun.

Upstream HTTP 400–499, including 429, releases its reservation to zero and
keeps the recorded failure plus a release audit. 5xx, transport failures,
timeouts and invalid successful replies keep their reservations. For older
held 4xx rows an operator can call the Python-only
`ledger.release_refused_reservations(authorization="Reviewed old 4xx refusals")`.
It releases only unsettled recorded 4xx, audits the authorization/count and
each release, and does not lift a stop; `authorize_resume` remains separate.
There is no release CLI.

`GET /editor/v1/models` with the client token returns the configured model
as an OpenAI `object: list` of `object: model`, `owned_by: lab-gateway`
entries. A group's catalogue accepts any member's client token and lists all
member models in declared order; completions require the selected model's
token. Listing stays local and makes no reservation. Unknown roles get
`404 invalid_role_or_model`, wrong or missing tokens
`401 invalid_gateway_credentials`. See the
[accounting pitfalls](../pitfalls/SKILL.md#gateway-accounting).

Items: {req:campaign.models}, {req:campaign.brief}.

<a id="run-failures"></a>
## Failures during execution {req:run.failures}

Choose before starting a campaign or fixed evaluation. The variant describes the
following explicit values; `max_turns` is the selected per-conversation budget.
For `campaign.indeterminate.<каждый>`, apply the values to every owner.

| | `retry` «С повтором» (умолчание) | `no_retry` «Без повтора» | `fail_fast` «Остановка на первом сбое» |
|---|---|---|---|
| `campaign.sample_retries` | 3 | 0 | 0 |
| `campaign.indeterminate.<каждый>.max_count` | 20 | 20 | 0 |
| `campaign.indeterminate.<каждый>.max_rate` | `"1"` | `"1"` | `"0"` |
| `reflector.max_consecutive_failures` | 5 | 5 | 1 |
| `campaign.call_retries.max_attempts` | 3 | 3 | 1 |
| `campaign.call_retries.turn_max_attempts` | 2 | 1 | 1 |
| `campaign.budget.max_failed_turns` | = `max_turns` | = `max_turns` | 1 |
| оценка: `sample_retries` | 3 | 0 | 0 |
| оценка: `min_coverage` | 0.95 | 0.95 | 1 |
| оценка: `max_consecutive_unmeasured` | 3 | 3 | 1 |
| оценка: `call_retries` | 3 / 2 | 3 / 1 | 1 / 1 |
| оценка: `max_failed_turns` | = `max_turns` | = `max_turns` | 1 |

**`retry` «С повтором» (default).** Unmeasured samples can be attempted again,
transient target calls can be retried, and failed turns can continue within the
conversation budget. Scored coverage still has to reach 0.95. Setting
`sample_retries > 0` or `turn_max_attempts > 1` asserts that resending a question
to the agent is safe; confirm that before choosing these values. Retries can
repeat side effects and add work. Turn retries require the target to resend the
same `request_id`; see [Turn outcomes](../target-connector/SKILL.md#turn-outcomes).

**`no_retry` «Без повтора».** Use this for agents whose repeated turn changes
the world. Sample and turn resending are disabled; transient-call attempts stay
at 3 with `turn_max_attempts: 1`. A failed sample may remain unmeasured while
other samples continue, subject to coverage and consecutive-unmeasured limits.
This setting is not an exactly-once guarantee for external side effects.

**`fail_fast` «Остановка на первом сбое».** Sample and turn retries are disabled;
one failed turn ends its rollout, and one consecutive unmeasured sample stops a
fixed evaluation. The campaign's indeterminate allowance is zero and evaluation
requires full scored coverage. An execution failure gives no score for that sample;
it is not evidence of poor quality.

On exact Core b22, `max_rate < 1` can make a failure on the first questions fatal:
Core counts the rate from the first denominator, so the first failed try has
rate 1 even if `max_count` is still below its limit. Optional manifest `/16`
`campaign.indeterminate.<owner>.min_tries` provides a warm-up before the rate
check; Lab does not write it or other `/16` settings for you.

The builder writes campaign keys into the immutable manifest; Lab does not
substitute them or check the manifest against the chosen variant. Lab's neutral
campaign scaffold writes `retry`. Fixed evaluation has its own request/input
settings and does not inherit campaign failure settings. New preparations use
`retry` for omitted/default-null evaluation values; explicit values override them.
See [Evaluation budget](../evaluation-inputs/SKILL.md#evaluation-budget).

Changing the variant before start creates a new Bundle, but does not by itself
require a new first measurement when the measurement basis stays equal. Use
[measurements for a Bundle](../service/README.md#bundle-measurements) and the shared
project facts to assess saved evidence. Running or suspended campaigns do not
change variant in place. Start a new campaign and choose its first baseline using the project start options above.

<a id="campaign-scale"></a>
## Campaign scale

| Pointer | Meaning |
|---|---|
| `/campaign/rounds` | Number of rounds. |
| `/campaign/attempts_per_round` | Edit attempts per round. |
| `/campaign/parallel_attempts` | Attempts run at the same time. |
| `/campaign/max_concurrency` | Ceiling on concurrent rollouts across the whole campaign (a semaphore taken before each rollout starts). |
| `/campaign/max_scorer_concurrency` | Ceiling on concurrent scorer calls across the whole campaign. |
| `/campaign/budget` | Per-sample limits `max_turns` and `timeout_seconds`; failure settings are described in [Failures during execution](#run-failures). It is not money. |

<a id="target-capacity"></a>
Parallelism is also bounded by the target's declared capacity; see
[Capacity](../target-connector/SKILL.md#capacity). For `/15`, Lab asks core's own
`admit_capabilities` for the least accepted `max_instances`; it keeps no formula of its
own. If the launch file declares less, preparation refuses `artifacts_invalid` with
`target capacity: the campaign needs N instances, the target declares M`.
The levers include `max_concurrency`, `parallel_attempts`, `gate.fresh_parent`,
`attempts_per_round` and the target's actual `max_concurrent_sessions`.
The scaffold's observe smoke profile (`max_concurrency: 2`, `parallel_attempts: 1`,
`fresh_parent: true`) needs 4 instances when each instance serves one session.
Item: {req:campaign.scale}.

Lab does not track spend or enforce a money limit in this release
({req:campaign.budget}).

Choose retries, failed-turn limits and indeterminate allowances through
[Failures during execution](#run-failures), separately from campaign scale.
Fixed evaluation has its own resolved budget, never inherited from the campaign
manifest; see [Evaluation inputs](../evaluation-inputs/SKILL.md#evaluation-budget).

## Evaluation-only path: `lab-evaluation-input/1`, `/2` or `/3`

For measuring an agent without changing it, write a closed
`lab-evaluation-input/1`, `lab-evaluation-input/2` or `lab-evaluation-input/3` instead of a manifest.
Its keys are exactly:

| Key | Content |
|---|---|
| `schema` | `lab-evaluation-input/1`, `lab-evaluation-input/2` or `lab-evaluation-input/3` |
| `seed_root` | Directory of the tree under test. |
| `policy` | `{version, excludes, dependency_paths, writable_paths}` artifact policy. |
| `target` | Target binding, including `seed_mode`. |
| `corpus` | `{path}` |
| `environment` | `{kind, input_text_key}` |
| `scorer` | `{kind: "subprocess", argv, cwd, dependency_paths, allowed_env, timeout_seconds}` |
| `evaluation_set_id` | Name of this evaluation set. |
| `metric_plan` | For example `{"primary": "score"}`. |
| `defaults` | `{repeat_seeds, max_turns, timeout_seconds}`; repeat seeds are integers for `exact` seed mode and `null` for `none`/`unpaired`. `/2` also accepts the optional `max_failed_turns` and `call_retries` (see [Evaluation budget](../evaluation-inputs/SKILL.md#evaluation-budget)); `/1` refuses them. All three majors accept sample retries, coverage and concurrency; `/3` also accepts `max_consecutive_unmeasured` (positive integer or `"off"`). |

There is no editor or reflector on this path. The launch file's `policy`
points to this input before bundling; Lab generates a separate conformance
manifest inside the Bundle.

## From documents to an operation

1. **Bundle.** Create a Bundle from the manifest (or evaluation input), the
   launch file, the ordered environments, explicit roots, `env`, secret names
   and path bindings. See the [preparation guide](../preparation/SKILL.md).
2. **PreparedExecution.** Prepare the campaign, or a fixed evaluation with an
   explicit selection. Preparation runs nothing.
3. **Start / Stop / cleanup.** Bind, start the target, start the operation,
   read status and report, stop, and clean up explicitly. See the
   [Owner guide](../owner/README.md), or the [service guide](../service/README.md)
   for the same steps through the local service.

Use a new private state directory for each producing Lab/core pair; never open
an older pair's state or copy its database or receipts into a new one.


Core storage failures such as `persistence_failed` appear as `core_terminal`
`{owner, terminal, code, hint}` in status/list `/8` and Owner report `/15`.
`hint` is bilingual `{ru, en}` when a repair hint is known, otherwise null.
Each `starts[]` entry keeps its own terminal; the top-level field copies the last
start. An accepted resume opens a start with null `core_terminal`. Successful
finishes, suspensions and refusals have null `core_terminal`; suspension and
refusal retain their existing fields. CLI status/watch print the terminal and
its hint. The closed Lab `failure` codes and cleanup rules are unchanged.
