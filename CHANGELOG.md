# Changelog

This beta snapshot preserves the supported workflows and stored-run contracts described below.

## [2.0.0b18] — 2026-11-10

This release keeps the manifest schemas, campaign families, wire protocol, exit codes and Python import paths. `/16` remains opt-in and the scaffold still writes `/15`.

### Added

- `/16` `campaign.evaluation.focus_repeats_per_question` measures each Attempt's focus case more often in the Candidate and fresh Parent; causal cases, baselines, completion, verification, Champion and refresh measurements keep their existing repeat count. It requires `gate.mode: point` and `gate.fresh_parent: true`. The point gate compares per-case means. See [focus repeats](docs/manifest-v2.md#focus-repeats).
- `/16` `reflector.selection.min_claim_cases` (default `2`) sets the smallest Reflector claim that becomes the gate's focus. Set it to `1` to allow a focus of one case. A smaller claim gets the gate source `below-minimum`.

### Changed

- `/16` `read_file` returns file text once in `content` and drops `line_numbered_content`. Chat Editor and Reflector tool schemas become `chat_tools/3` and `reflect_tools/4`; the agent-CLI Editor stays at `chat_tools/2`.
- `/16` default Reflector prompts explain that the gate's focus is the union of `affected_cases` across hypotheses and ask the Reflector to claim only what it can fix. The defaults are `tree_reflector_v6.md` and `tree_agent_reflector_v4.md`.
- `/16` default Editor prompts ask the Editor to read related instructions and examples, reconcile competing rules in the same edit, stay inside its writable paths and re-read the changed regions. The defaults are `tree_editor_v3.md` and `tree_agent_editor_v2.md`. A declared `system_prompt` takes precedence. `/15` prompts remain unchanged.
- Scorer stream buffers and the Reflector's Parent projection use the default temporary directory (`TMPDIR`, else `/tmp`). Storage-fault remedies name that directory. Point `TMPDIR` at a disk directory if `/tmp` is a small tmpfs. Journals, pack bytes and identities stay unchanged. See [disk and inode faults](docs/running-campaigns.md#when-the-disk-or-its-inodes-run-out).
- Per-attempt workspace copies are no longer fsynced. Recovery resets a kept workspace from its verified Parent or abandons it; published Candidates are still fsynced. A power loss during sealing can therefore more often abandon the Attempt as `seal_not_published`.
- A `/16` command Reflector program failure records `program_failed` instead of `unclassified`, with failure class `unknown`. `/15` retains its earlier behavior.

### Fixed

- `/16` chat Editor, Reflector and Grouper portion sessions retain `{}` instead of broken tool-call arguments in subsequent assistant history. Non-object JSON, markup, truncated text and empty arguments remain invalid for execution. Valid object arguments, call IDs, names, order, content and reasoning fields are preserved. Corrections identify removed arguments by length and hash without a preview; original arguments remain in the private role log. `/15` keeps the previous repair behavior.
- Run secrets in reported `used` unions or component IDs no longer stop `/15` and `/16` runs with `persistence_failed`. A secret-bearing union becomes unknown provenance. Secret-bearing component IDs are omitted, counted by optional positive `components_withheld`; a withheld map on either side selects every case with fallback reason `components_withheld`. Warnings contain counts only. Runs without these matches keep their journal bytes.
- `/16` `read_evidence` accepts the end of a record without a final newline and returns empty `content` with `next_offset: null`. Offsets inside a line or past the end remain invalid.
- A `/16` chat Editor that deletes a file and restores its original bytes now restores its baseline mode and frees its changed-file budget slot.
- Subprocess Editor and Reflector stderr logs redact the run's secret set. Logs are written after the program ends; later background-process writes are not logged. A Reflector with a probe retains its streamed log. Scorer traces and command Grouper stderr remain raw.
- `conformance_manifest` refuses a non-mapping input as `bad_type` at `$`.
- Tree copies use `os.copy_file_range` on Linux interpreters that provide it before Python 3.14. Digest checks and later fsyncs remain in place. Interpreters without that function retain the earlier copy path.

### Upgrade

- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to refuse earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 stay refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

- Install the same version at both wire ends. Unfinished b17 runs require one `run_identity_changed` authorization with effect `rebase`; every run lists `producing_version`. `/16` chat Editor and Reflector runs also list their role digest, including with a declared `system_prompt`. `/16` agent-CLI roles list their digest only with the default prompt. `/15` roles and `/16` chat Grouper or subprocess roles add no role identity change. Accept the printed differences with `evo tree authorize`, then repeat the same command. Finish or stop every `/15` run before upgrading.
- Frozen Attempts resume with their frozen inputs. An open reflection closes as `reflection_failed` with reason `interrupted` and restarts in the same round. Finished grouping remains bound; an interrupted grouping can adopt its finished portions.
- The two new optional keys enter identity only when declared. Adding `min_claim_cases` requires an adaptation `rebase`. Adding `focus_repeats_per_question` changes policy and offers `rebaseline` or `close`, or only `close` when no round remains.
- Rollback to b17 refuses manifests with either new key as `unknown_key`. A campaign journal containing `program_failed` or `components_withheld` cannot be resumed by b17 and ends `persistence_failed` before authorization. When `components_withheld` occurs only in measurement journals, b17 may append the usual `identity_rebased` first and then fail reading the measurement journal. Reinstall b18 to continue; another `producing_version` rebase may be needed if b17 rebased the run.

### Retained limitations

- Secrets in component `sources` paths still trigger the journal guard and end the run `persistence_failed`; keep secrets out of source paths.
- A lineage import does not screen predecessor `used` unions against the successor's run secrets. A union matching a successor secret can still stop the import with `persistence_failed`.
- `/15` and agent-CLI Editors retain the writer's mode when recreating a file. A declared Editor `system_prompt` does not gain the default reconciliation instructions.

## [2.0.0b17] — 2026-11-08

### Added

- Newly formed `/16` model groups retain their whole observed failure, expectation and investigation question in bound records when map descriptions overflow. Short map entries retain the whole question; fields descriptions use the same derived 1,568-character bound. `/15` keeps its 512-character bound.

- Import `evaluate` and `inspect_evaluation` from `evolution.tree` before the evaluation package move. These eager root exports are the existing canonical functions, appended after the 130 existing aggregate names. Maintained evaluation examples use the root imports.

- `/16` chat roles gain `require_parameters` (default `true`), chat Editor and Reflector gain `nearest_paths` (default `5`), and campaign policy gains `reflector_context` and `context_max_bytes` (default `65536`). Each new key enters identity only when declared. See the [manifest reference](docs/manifest-v2.md).

- The `modern_single` tool style requires a tool call while omitting `parallel_tool_calls`. Pinned roles can negotiate a request shape the provider accepts after exhausting a refused style's retries.

- Router refusals get a private `provider_refused` record. The router message stays there, uncut after run-secret redaction; other surfaces carry safe status and codes only.

- New `/16` Editor and Reflector prompts state tool contracts and session limits. Tool results name dropped parallel calls, evidence filters accept case IDs or handles, and reader and path errors name valid values or nearby readable files.

- Newly formed `/16` fields groups and fields fallbacks carry bounded descriptions of their grouping keys and an investigation question. Text that cannot fit or pass the checks is omitted without failing the grouping.

#### Evaluation import migration

| Old import | New import |
|---|---|
| `evolution.tree.evaluation:evaluate` | `evolution.tree:evaluate` |
| `evolution.tree.evaluation:inspect_evaluation` | `evolution.tree:inspect_evaluation` |

### Changed

- Move the shared frozen `EvaluationPolicy` value to execution configuration. Campaign measurements and standalone evaluation use the same class, with unchanged fields, constructor, schedules and persisted identities. The old defining path is retired; consumers use a private module binding instead of re-exporting the class. Migrate deep imports using this table; the eager root exports retain their names and order.

| Old import | New import |
|---|---|
| `evolution.tree.evaluation_manifest:EvaluationPolicy` | `evolution.tree.execution.config:EvaluationPolicy` |

- Extract the existing internal `DurableReference` contract and its closed validation dependencies from campaign grammar into `evolution.tree.execution.durable_reference` into the execution package. Target preflight and campaign replay use the same class. The eager root initializer and its ordered aggregate exports are unchanged. Payloads, validation diagnostics and persisted evidence are unchanged. Migrate deep defining imports using the table below.

#### Durable reference import migration

| Old import | New import |
|---|---|
| `evolution.tree.campaign_schema:DurableReference` | `evolution.tree.execution.durable_reference:DurableReference` |
| `evolution.tree.campaign_schema:EVIDENCE_KINDS` | `evolution.tree.execution.durable_reference:EVIDENCE_KINDS` |
| `evolution.tree.campaign_schema:_LOCATOR` | `evolution.tree.execution.durable_reference:_LOCATOR` |
| `evolution.tree.campaign_schema:_SHA256` | `evolution.tree.execution.durable_reference:_SHA256` |
| `evolution.tree.campaign_schema:_refuse` | `evolution.tree.execution.durable_reference:_refuse` |
| `evolution.tree.campaign_schema:_sha` | `evolution.tree.execution.durable_reference:_sha` |
| `evolution.tree.campaign_schema:_validate_locator` | `evolution.tree.execution.durable_reference:_validate_locator` |

- Move authorization values, digests and decision-file storage to `evolution.tree.execution.authorizations`. Campaign code retains their consequences. Root exports, decision bytes, refusal codes and CLI behavior stay unchanged.

#### Authorization migration table

| Old path | New path | Consumer |
|---|---|---|
| `evolution.tree.authorizations:ADAPTATION_PROFILE_FIELD` | `evolution.tree.execution.authorizations:ADAPTATION_PROFILE_FIELD` |  |
| `evolution.tree.authorizations:AUTHORIZABLE_CODES` | `evolution.tree.execution.authorizations:AUTHORIZABLE_CODES` |  |
| `evolution.tree.authorizations:AUTHORIZATIONS_DIR` | `evolution.tree.execution.authorizations:AUTHORIZATIONS_DIR` |  |
| `evolution.tree.authorizations:Authorization` | `evolution.tree.execution.authorizations:Authorization` |  |
| `evolution.tree.authorizations:AuthorizationConflict` | `evolution.tree.execution.authorizations:AuthorizationConflict` |  |
| `evolution.tree.authorizations:AuthorizationError` | `evolution.tree.execution.authorizations:AuthorizationError` |  |
| `evolution.tree.authorizations:COMPARABLE_FIELDS` | `evolution.tree.execution.authorizations:COMPARABLE_FIELDS` |  |
| `evolution.tree.authorizations:DecisionEffect` | `evolution.tree.execution.authorizations:DecisionEffect` |  |
| `evolution.tree.authorizations:EFFECT_LEADS` | `evolution.tree.execution.authorizations:EFFECT_LEADS` |  |
| `evolution.tree.authorizations:EFFECT_SENTENCES` | `evolution.tree.execution.authorizations:EFFECT_SENTENCES` | |
| `evolution.tree.authorizations:Effect` | `evolution.tree.execution.authorizations:Effect` |  |
| `evolution.tree.authorizations:GUARD_CLASSES` | `evolution.tree.execution.authorizations:GUARD_CLASSES` |  |
| `evolution.tree.authorizations:HARD_CODES` | `evolution.tree.execution.authorizations:HARD_CODES` |  |
| `evolution.tree.authorizations:HARD_REASONS` | `evolution.tree.execution.authorizations:HARD_REASONS` |  |
| `evolution.tree.authorizations:ISSUED_AT` | `evolution.tree.execution.authorizations:ISSUED_AT` |  |
| `evolution.tree.authorizations:MAX_DECISIONS` | `evolution.tree.execution.authorizations:MAX_DECISIONS` |  |
| `evolution.tree.authorizations:MAX_DECISION_BYTES` | `evolution.tree.execution.authorizations:MAX_DECISION_BYTES` |  |
| `evolution.tree.authorizations:MAX_REASON_CHARS` | `evolution.tree.execution.authorizations:MAX_REASON_CHARS` |  |
| `evolution.tree.authorizations:RefusalClass` | `evolution.tree.execution.authorizations:RefusalClass` |  |
| `evolution.tree.authorizations:RefusalDifference` | `evolution.tree.execution.authorizations:RefusalDifference` |  |
| `evolution.tree.authorizations:SCALE_CHANGE_SENTENCE` | `evolution.tree.execution.authorizations:SCALE_CHANGE_SENTENCE` |  |
| `evolution.tree.authorizations:SCHEMA` | `evolution.tree.execution.authorizations:SCHEMA` |  |
| `evolution.tree.authorizations:TEMPORARY_PREFIX` | `evolution.tree.execution.authorizations:TEMPORARY_PREFIX` |  |
| `evolution.tree.authorizations:_EFFECT_VALUES` | `evolution.tree.execution.authorizations:_EFFECT_VALUES` |  |
| `evolution.tree.authorizations:_SHA` | `evolution.tree.execution.authorizations:_SHA` |  |
| `evolution.tree.authorizations:_STORED` | `evolution.tree.execution.authorizations:_STORED` |  |
| `evolution.tree.authorizations:_leaf` | `evolution.tree.execution.authorizations:_leaf` |  |
| `evolution.tree.authorizations:_read_fd` | `evolution.tree.execution.authorizations:_read_fd` |  |
| `evolution.tree.authorizations:_staged_file` | `evolution.tree.execution.authorizations:_staged_file` |  |
| `evolution.tree.authorizations:_triple` | `evolution.tree.execution.authorizations:_triple` |  |
| `evolution.tree.authorizations:baseline_import_instance_digest` | `evolution.tree.execution.authorizations:baseline_import_instance_digest` |  |
| `evolution.tree.authorizations:canonical_json` | `evolution.tree.execution.authorizations:canonical_json` |  |
| `evolution.tree.authorizations:comparable_changed` | `evolution.tree.execution.authorizations:comparable_changed` |  |
| `evolution.tree.authorizations:comparable_field` | `evolution.tree.execution.authorizations:comparable_field` |  |
| `evolution.tree.authorizations:hint_command` | `evolution.tree.execution.authorizations:hint_command` |  |
| `evolution.tree.authorizations:hint_command_accept` | `evolution.tree.execution.authorizations:hint_command_accept` |  |
| `evolution.tree.authorizations:hygiene_instance_digest` | `evolution.tree.execution.authorizations:hygiene_instance_digest` |  |
| `evolution.tree.authorizations:identity_differences` | `evolution.tree.execution.authorizations:identity_differences` |  |
| `evolution.tree.authorizations:identity_instance_digest` | `evolution.tree.execution.authorizations:identity_instance_digest` |  |
| `evolution.tree.authorizations:identity_record` | `evolution.tree.execution.authorizations:identity_record` |  |
| `evolution.tree.authorizations:lineage_instance_digest` | `evolution.tree.execution.authorizations:lineage_instance_digest` |  |
| `evolution.tree.authorizations:match` | `evolution.tree.execution.authorizations:match` |  |
| `evolution.tree.authorizations:read_decisions` | `evolution.tree.execution.authorizations:read_decisions` |  |
| `evolution.tree.authorizations:read_decisions_fd` | `evolution.tree.execution.authorizations:read_decisions_fd` |  |
| `evolution.tree.authorizations:refusal_class` | `evolution.tree.execution.authorizations:refusal_class` |  |
| `evolution.tree.authorizations:validate_issued_at` | `evolution.tree.execution.authorizations:validate_issued_at` |  |
| `evolution.tree.authorizations:write_decision` | `evolution.tree.execution.authorizations:write_decision` |  |

### Fixed

- `/16` pinned chat roles negotiate HTTP-2xx router error bodies as refusals, before identity checks or consolidation parsing. Credential and transport failures retain their retry and failure routes. The family-15 chat Grouper follows its existing contract of not verifying completion identity.

### Upgrade

- On release, an unfinished `/16` run with a chat Editor or Reflector reports `run_identity_changed` once, with effect `rebase`, listing the changed role identity and `producing_version`, even with a declared `system_prompt`. Other runs list `producing_version` only when no configuration changed. Accept the printed finding and repeat the same command; the release probes will verify the exact fields.

- A frozen Attempt or open reflection resumes with its inputs. A finished grouping keeps its result and group IDs; later groupings receive fields descriptions.

- `require_parameters: false` trades the parameter guarantee for availability: a provider may silently ignore unsupported parameters.

- `/15` retains its previous bytes and its known limitation of reporting an error body as `provider_changed` under a pin.

## [2.0.0b16] — 2026-10-31

### Added

- **The `/16` chat Grouper groups in portions with read tools.** It uses the protocol `model_grouping/2` and the prompt `tree_grouping_model_v2.md`. Failing observations are split into portions. Each portion is a short tool session (`read_observation`, `read_trace`, `list_case`, `finish_portion`). A cut answer splits the portion. Groups from several portions are consolidated in batches. Every request stays within `max_input_chars` and `max_output_tokens`. The chat Grouper sees per-component counts of reported provenance. See [chat Grouper settings](docs/manifest-v2.md#the-chat-grouper-in-portions-16).

- New optional `/16` Grouper keys: `group_output_tokens`, `glimpse_chars`, `max_set_members`, `read_chars`, `max_turns`, `card_examples`, `max_reduce_levels` and `max_requests`. Every key except `max_requests` has a default (`max_requests` has none: no cap); none has an upper bound and enters identity only when declared. `/15` refuses them as `unknown_key`.

- Finished portions and consolidation batches are checkpointed (`grouping_portion_finished`, `grouping_batch_finished`). An interrupted grouping keeps its paid work: the next run adopts the checkpoints that still match.

### Fixed

- Grouper response records in the private log now carry `finish_reason` and `usage`, in both families.

- A round's finished grouping stays bound across a Grouper rebase, in both families, so a rebase no longer makes an open round's grouping "not reproducible".

- A lifecycle record refused during grouping (`CampaignInvariantViolated`) now propagates as an executor fault, in both families, instead of being journaled as `grouping_failed` with `malformed_result`. The grouping stays open and the next run settles it as interrupted; in family 15 its checkpoints stay adoptable.

### Upgrade

- **Upgrade to `2.0.0b16`.** Install the same release at both wire ends. An unfinished `/16` run with a `chat` Grouper is refused once with `run_identity_changed` (effect `rebase`), listing `adaptation` and `producing_version`; one `evo tree authorize` `rebase` accepts both, then repeat the same `evo tree evolve` command. Every other unfinished run, `/15` (including a `/15` chat Grouper) or `/16` with a `fields` or `command` Grouper, is refused once listing only `producing_version` and continues after one `rebase`; its identity and adaptation bytes are unchanged. In the release probes this held for 35 kinds of unfinished `2.0.0b15` runs, among them runs interrupted after `grouping_started`, `grouping_finished`, `editor_started` and `round_closed` with a chat Grouper, `command` Grouper runs, chat and subprocess Editors and Reflectors, a declared probe, `focus_first` and unfinished runs with `insufficient_evidence` Attempts: each finished `completed`, and `verify_evidence`, the audit and `inspect_campaign(..., verify=True)` found no mismatch. A finished run stays finished: `evo tree evolve` refuses it with `run_identity_changed` and no effect, as for any finished run under another release, while `inspect_campaign(..., verify=True)` and `evo tree watch --once` read a `2.0.0b15` `/16` chat Grouper run under this release with no mismatch. The general rule still applies: finish or stop (Ctrl+C) every `/15` run before upgrading.

- **What a rebased `/16` chat Grouper run regroups.** A round that already has its own finished grouping keeps it (its `model_grouping/1` groups and group ids do not change) and asks the Grouper nothing more. A grouping that was interrupted, and every later round, groups again under `model_grouping/2`.

- A declared Grouper `system_prompt` must describe the portion session and `finish_portion`. A prompt written for `model_grouping/1` gets `invalid_output` and the declared fallback.

- `timeout_seconds` now bounds one map session or one consolidation batch, not the whole grouping. Total grouping time grows with the data.

- `max_requests` caps the requests of one grouping. The preflight request counts toward it.

- With the packaged prompt and the default keys, a `/16` chat Grouper needs `max_input_chars` of at least 26,943: every request carries the system prompt, the portion lead and the tool definitions, and each portion keeps a reserve of `read_chars` (default 16,000), a 512-character result envelope and 600 characters for repairs. A smaller limit takes the declared fallback with `input_too_large` before any request. Leave room above it for the portions themselves.

- **An interrupted grouping resumes from its checkpoints.** The next run of the same command settles the open grouping as `interrupted`, adopts the finished portions and batches whose baseline binding, policy digest and plan digest still match, and asks only for the rest; the preflight request is sent again.

- **Rollback to `2.0.0b15` works for runs without a `/16` chat Grouper.** Such a `/15` or `/16` run resumes under `2.0.0b15` after one `rebase` listing `producing_version`, and finishes. `2.0.0b15` cannot read a `/16` run with a chat Grouper that this release created or rebased: its `campaign_created` or `identity_rebased` record carries `model_grouping/2`, and its `grouping_portion_finished` records are unknown to `2.0.0b15`. The resume ends with `persistence_failed` (`campaign_journal`, `journal_unavailable`) and exit `1`, before any authorization is asked, and writes nothing into the run directory. Install `2.0.0b16` again and repeat the same command; the run continues without another authorization.

- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to refuse earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 stay refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

The supported tree commands include `evo tree evolve` and `evo tree conformance`; `evo-target` provides the separate offline demonstration. Files using `evolution-v2-manifest/5` are unsupported; use /15 or /16. There is no `evo gui`, `[gui]` extra or `evo tree edit`. Refresh editable package metadata after a version change to avoid `package_version_mismatch`.

## [2.0.0b15] — 2026-10-29

### Added

- **`/16` focus-first gate stage.** The optional key `campaign.gate.stages` takes `single` or `focus_first`. With `focus_first`, an Attempt measures its focus cases first, at all R repeats. If focus does not pass, the Attempt closes `rejected` with `deciding_scope: focus`, its causal scope is reported `not_measured`, and no causal case is run for the Candidate or a fresh Parent. If focus passes, the causal stage runs, and a finalist still has every focus and causal slot measured. With `point`, `confidence` or `unfiltered` and a numeric causal tolerance, the numbers and the Attempt outcome equal those of `single` over the same rows; the rejection reason can differ, and seeded quantities (`measured_noise` calibration, the `statistical` bootstrap) can differ because the policy digest differs. Absent or `single` keeps `2.0.0b14` behavior, policy digest and record bytes. `/15` refuses the key. See and .

- Runs that declare `focus_first` write a new `focus_decided` record before any causal case runs, `scope` (`focus` or `causal`) on `stage_opened`, and a causal scope status `not_measured` in the comparison. Their status and result show `settings.stages`, and each Attempt shows `slots` per side (scheduled, dispatched and not owed), so an operator sees what focus-first saved. `watch` names the stage and scope that is running. Changing `stages` on an unfinished run is a policy change that offers `rebaseline` or `close`; an accepted `continue` after a focus decision carries that decision and never recomputes it.

### Fixed

- **`evo tree evolve` reports an invalid projection instead of a traceback.** If a public projection still fails validation after the terminal record is durable, the result document has `code: projection_invalid`, `owner: campaign_projection`, `result: null` and a minimal `status`, and the command exits `1`. The recorded terminal is unchanged. With a fixed Core, a suspended run resumes with the same command, and a finished run is read with `evo tree watch --once`; another release does not reopen a finished run ([CLI reference](docs/cli.md), amendment of 2026-10-29). A projection error before any terminal is durable is raised as before, and a projection that would publish a run secret is still refused. The result code is listed in and the [CLI reference](docs/cli.md).

### Changed

- Move execution support into `evolution.tree.execution`. Keep the existing root imports and persisted formats.

#### Execution import migration

| Old import | New import | |
|---|---|---|
| `evolution.tree.bindings:CorpusError` | `evolution.tree.execution.corpus:CorpusError` | |
| `evolution.tree.bindings:EvolveRefused` | `evolution.tree.execution.output_admission:EvolveRefused` | |
| `evolution.tree.bindings:FAMILY14_OUTPUT_ROOTS` | `evolution.tree.execution.output_admission:FAMILY14_OUTPUT_ROOTS` |  |
| `evolution.tree.bindings:HygieneFinding` | `evolution.tree.execution.output_admission:HygieneFinding` |  |
| `evolution.tree.bindings:OUTPUT_ROOTS` | `evolution.tree.execution.output_admission:OUTPUT_ROOTS` |  |
| `evolution.tree.bindings:PROCESS_LOGS_DIR` | `evolution.tree.execution.output_admission:PROCESS_LOGS_DIR` |  |
| `evolution.tree.bindings:ReflectionCorpus` | `evolution.tree.execution.corpus:ReflectionCorpus` |  |
| `evolution.tree.bindings:RefusalFinding` | `evolution.tree.execution.output_admission:RefusalFinding` | |
| `evolution.tree.bindings:_ALLOWED_CASE_KEYS` | `evolution.tree.execution.corpus:_ALLOWED_CASE_KEYS` |  |
| `evolution.tree.bindings:_MAX_REFLECTION_METADATA_BYTES` | `evolution.tree.execution.corpus:_MAX_REFLECTION_METADATA_BYTES` |  |
| `evolution.tree.bindings:_MAX_REFLECTION_METADATA_DEPTH` | `evolution.tree.execution.corpus:_MAX_REFLECTION_METADATA_DEPTH` |  |
| `evolution.tree.bindings:_MAX_REFLECTION_METADATA_KEYS` | `evolution.tree.execution.corpus:_MAX_REFLECTION_METADATA_KEYS` |  |
| `evolution.tree.bindings:_OPTIONAL_CASE_KEYS` | `evolution.tree.execution.corpus:_OPTIONAL_CASE_KEYS` |  |
| `evolution.tree.bindings:_REQUIRED_CASE_KEYS` | `evolution.tree.execution.corpus:_REQUIRED_CASE_KEYS` |  |
| `evolution.tree.bindings:_canonical_metadata_json` | `evolution.tree.execution.corpus:_canonical_metadata_json` |  |
| `evolution.tree.bindings:_finding` | `evolution.tree.execution.output_admission:_finding` |  |
| `evolution.tree.bindings:_freeze_metadata` | `evolution.tree.execution.corpus:_freeze_metadata` |  |
| `evolution.tree.bindings:_judge_output_root` | `evolution.tree.execution.output_admission:_judge_output_root` |  |
| `evolution.tree.bindings:_load_corpus` | `evolution.tree.execution.corpus:_load_corpus` |  |
| `evolution.tree.bindings:_metadata_depth` | `evolution.tree.execution.corpus:_metadata_depth` |  |
| `evolution.tree.bindings:_metadata_payload` | `evolution.tree.execution.corpus:_metadata_payload` |  |
| `evolution.tree.bindings:_reflection_case` | `evolution.tree.execution.corpus:_reflection_case` |  |
| `evolution.tree.bindings:_reflection_metadata` | `evolution.tree.execution.corpus:_reflection_metadata` |  |
| `evolution.tree.bindings:_refuse_or_admit` | `evolution.tree.execution.output_admission:_refuse_or_admit` |  |
| `evolution.tree.bindings:admit_output_root` | `evolution.tree.execution.output_admission:admit_output_root` |  |
| `evolution.tree.bindings:collect_output_root_findings` | `evolution.tree.execution.output_admission:collect_output_root_findings` |  |
| `evolution.tree.bindings:load_eval_corpus` | `evolution.tree.execution.corpus:load_eval_corpus` | |
| `evolution.tree.bindings:load_reflection_corpus` | `evolution.tree.execution.corpus:load_reflection_corpus` |  |
| `evolution.tree.bindings:output_roots` | `evolution.tree.execution.output_admission:output_roots` |  |
| `evolution.tree.call_failures:CONTRACT_CODES` | `evolution.tree.execution.call_failures:CONTRACT_CODES` |  |
| `evolution.tree.call_failures:FailureClass` | `evolution.tree.execution.call_failures:FailureClass` |  |
| `evolution.tree.call_failures:STRUCTURAL_CODES` | `evolution.tree.execution.call_failures:STRUCTURAL_CODES` |  |
| `evolution.tree.call_failures:TRANSIENT_CODES` | `evolution.tree.execution.call_failures:TRANSIENT_CODES` |  |
| `evolution.tree.call_failures:_typed` | `evolution.tree.execution.call_failures:_typed` |  |
| `evolution.tree.call_failures:classify_failure` | `evolution.tree.execution.call_failures:classify_failure` |  |
| `evolution.tree.call_failures:may_retry` | `evolution.tree.execution.call_failures:may_retry` |  |
| `evolution.tree.check_modes:CHECKS_VERSION` | `evolution.tree.execution.check_modes:CHECKS_VERSION` |  |
| `evolution.tree.check_modes:CHECK_NAMES` | `evolution.tree.execution.check_modes:CHECK_NAMES` |  |
| `evolution.tree.check_modes:CODES` | `evolution.tree.execution.check_modes:CODES` |  |
| `evolution.tree.check_modes:CONSEQUENCES` | `evolution.tree.execution.check_modes:CONSEQUENCES` | |
| `evolution.tree.check_modes:CheckModes` | `evolution.tree.execution.check_modes:CheckModes` |  |
| `evolution.tree.check_modes:ENFORCE_ALL` | `evolution.tree.execution.check_modes:ENFORCE_ALL` |  |
| `evolution.tree.check_modes:HitSink` | `evolution.tree.execution.check_modes:HitSink` |  |
| `evolution.tree.check_modes:INPUT_CHECKS` | `evolution.tree.execution.check_modes:INPUT_CHECKS` |  |
| `evolution.tree.check_modes:Mode` | `evolution.tree.execution.check_modes:Mode` |  |
| `evolution.tree.check_modes:ShadowEntry` | `evolution.tree.execution.check_modes:ShadowEntry` |  |
| `evolution.tree.check_modes:_DEFERRED` | `evolution.tree.execution.check_modes:_DEFERRED` |  |
| `evolution.tree.check_modes:_FIELD` | `evolution.tree.execution.check_modes:_FIELD` |  |
| `evolution.tree.check_modes:_SILENT` | `evolution.tree.execution.check_modes:_SILENT` |  |
| `evolution.tree.check_modes:_Value` | `evolution.tree.execution.check_modes:_Value` |  |
| `evolution.tree.check_modes:_field_values` | `evolution.tree.execution.check_modes:_field_values` |  |
| `evolution.tree.check_modes:_primitive_tokens` | `evolution.tree.execution.check_modes:_primitive_tokens` |  |
| `evolution.tree.check_modes:_string_tokens` | `evolution.tree.execution.check_modes:_string_tokens` |  |
| `evolution.tree.check_modes:check_field` | `evolution.tree.execution.check_modes:check_field` |  |
| `evolution.tree.check_modes:check_predicate` | `evolution.tree.execution.check_modes:check_predicate` |  |
| `evolution.tree.check_modes:combine_checks` | `evolution.tree.execution.check_modes:combine_checks` |  |
| `evolution.tree.check_modes:grouping_field_key` | `evolution.tree.execution.check_modes:grouping_field_key` |  |
| `evolution.tree.check_modes:grouping_field_locations` | `evolution.tree.execution.check_modes:grouping_field_locations` |  |
| `evolution.tree.check_modes:parse_checks` | `evolution.tree.execution.check_modes:parse_checks` | |
| `evolution.tree.check_modes:redact_for_journal` | `evolution.tree.execution.check_modes:redact_for_journal` |  |
| `evolution.tree.check_modes:scan_grouping_value` | `evolution.tree.execution.check_modes:scan_grouping_value` |  |
| `evolution.tree.check_modes:scan_grouping_wire` | `evolution.tree.execution.check_modes:scan_grouping_wire` |  |
| `evolution.tree.command_binding:CommandBinding` | `evolution.tree.execution.command_binding:CommandBinding` | |
| `evolution.tree.command_binding:CommandChanged` | `evolution.tree.execution.command_binding:CommandChanged` | |
| `evolution.tree.command_binding:_MODULE_UNSUPPORTED` | `evolution.tree.execution.command_binding:_MODULE_UNSUPPORTED` |  |
| `evolution.tree.command_binding:_SEPARATED_OPERAND_LONG` | `evolution.tree.execution.command_binding:_SEPARATED_OPERAND_LONG` |  |
| `evolution.tree.command_binding:_SEPARATED_OPERAND_SHORT` | `evolution.tree.execution.command_binding:_SEPARATED_OPERAND_SHORT` |  |
| `evolution.tree.command_binding:_carries_module_option` | `evolution.tree.execution.command_binding:_carries_module_option` |  |
| `evolution.tree.command_binding:_drop_contained_pythonpath` | `evolution.tree.execution.command_binding:_drop_contained_pythonpath` |  |
| `evolution.tree.command_binding:_json_digest` | `evolution.tree.execution.command_binding:_json_digest` |  |
| `evolution.tree.command_binding:_resolve_executable` | `evolution.tree.execution.command_binding:_resolve_executable` |  |
| `evolution.tree.command_binding:_search_path` | `evolution.tree.execution.command_binding:_search_path` |  |
| `evolution.tree.command_binding:argv_operands` | `evolution.tree.execution.command_binding:argv_operands` |  |
| `evolution.tree.command_binding:command_paths` | `evolution.tree.execution.command_binding:command_paths` |  |
| `evolution.tree.command_binding:file_digest` | `evolution.tree.execution.command_binding:file_digest` |  |
| `evolution.tree.command_binding:is_python_module_command` | `evolution.tree.execution.command_binding:is_python_module_command` |  |
| `evolution.tree.corpus_partition:CorpusPartition` | `evolution.tree.execution.corpus_partition:CorpusPartition` |  |
| `evolution.tree.corpus_partition:_split_digest` | `evolution.tree.execution.corpus_partition:_split_digest` |  |
| `evolution.tree.corpus_partition:load_partition` | `evolution.tree.execution.corpus_partition:load_partition` | |
| `evolution.tree.corpus_partition:partition_notes` | `evolution.tree.execution.corpus_partition:partition_notes` |  |
| `evolution.tree.corpus_partition:validate_partition_descriptor` | `evolution.tree.execution.corpus_partition:validate_partition_descriptor` |  |
| `evolution.tree.evidence_settings:DEFAULT_EVIDENCE` | `evolution.tree.execution.evidence_settings:DEFAULT_EVIDENCE` | |
| `evolution.tree.evidence_settings:EVIDENCE_DEFAULTS` | `evolution.tree.execution.evidence_settings:EVIDENCE_DEFAULTS` |  |
| `evolution.tree.evidence_settings:EVIDENCE_MINIMUMS` | `evolution.tree.execution.evidence_settings:EVIDENCE_MINIMUMS` |  |
| `evolution.tree.evidence_settings:EVIDENCE_VERSION` | `evolution.tree.execution.evidence_settings:EVIDENCE_VERSION` |  |
| `evolution.tree.evidence_settings:EvidencePin` | `evolution.tree.execution.evidence_settings:EvidencePin` | |
| `evolution.tree.evidence_settings:EvidenceSettings` | `evolution.tree.execution.evidence_settings:EvidenceSettings` |  |
| `evolution.tree.evidence_settings:SettingsError` | `evolution.tree.execution.evidence_settings:SettingsError` |  |
| `evolution.tree.evidence_settings:_OBSERVATION_LIMITS` | `evolution.tree.execution.evidence_settings:_OBSERVATION_LIMITS` |  |
| `evolution.tree.evidence_settings:parse_evidence` | `evolution.tree.execution.evidence_settings:parse_evidence` |  |
| `evolution.tree.journal:EVENTS_FILE` | `evolution.tree.execution.journal:EVENTS_FILE` |  |
| `evolution.tree.journal:Journal` | `evolution.tree.execution.journal:Journal` |  |
| `evolution.tree.journal:LEASE_FILE` | `evolution.tree.execution.journal:LEASE_FILE` |  |
| `evolution.tree.journal:MAX_JOURNAL_BYTES` | `evolution.tree.execution.journal:MAX_JOURNAL_BYTES` |  |
| `evolution.tree.journal:MAX_RECORD_BYTES` | `evolution.tree.execution.journal:MAX_RECORD_BYTES` |  |
| `evolution.tree.journal:PARTIAL_FILE` | `evolution.tree.execution.journal:PARTIAL_FILE` |  |
| `evolution.tree.journal:__all__` | `evolution.tree.execution.journal:__all__` |  |
| `evolution.tree.journal:inspect_journal` | `evolution.tree.execution.journal:inspect_journal` |  |
| `evolution.tree.journal_identity:CONNECTION_PREFLIGHT_PROFILE` | `evolution.tree.execution.journal_identity:CONNECTION_PREFLIGHT_PROFILE` |  |
| `evolution.tree.journal_identity:IdentityError` | `evolution.tree.execution.journal_identity:IdentityError` |  |
| `evolution.tree.journal_identity:MAX_SAMPLE_RETRIES` | `evolution.tree.execution.journal_identity:MAX_SAMPLE_RETRIES` |  |
| `evolution.tree.journal_identity:RunIdentityV1` | `evolution.tree.execution.journal_identity:RunIdentityV1` |  |
| `evolution.tree.journal_identity:ScorerRecoveryMode` | `evolution.tree.execution.journal_identity:ScorerRecoveryMode` |  |
| `evolution.tree.journal_identity:_FIELDS` | `evolution.tree.execution.journal_identity:_FIELDS` |  |
| `evolution.tree.journal_identity:_OPTIONAL` | `evolution.tree.execution.journal_identity:_OPTIONAL` |  |
| `evolution.tree.journal_identity:_TARGET` | `evolution.tree.execution.journal_identity:_TARGET` |  |
| `evolution.tree.journal_identity:_case_repeats` | `evolution.tree.execution.journal_identity:_case_repeats` |  |
| `evolution.tree.journal_identity:_case_subset` | `evolution.tree.execution.journal_identity:_case_subset` |  |
| `evolution.tree.journal_identity:_check_case_repeats` | `evolution.tree.execution.journal_identity:_check_case_repeats` |  |
| `evolution.tree.journal_identity:_closed` | `evolution.tree.execution.journal_identity:_closed` |  |
| `evolution.tree.journal_identity:_digest` | `evolution.tree.execution.journal_identity:_digest` |  |
| `evolution.tree.journal_identity:_identifier` | `evolution.tree.execution.journal_identity:_identifier` |  |
| `evolution.tree.journal_identity:_json_value` | `evolution.tree.execution.journal_identity:_json_value` |  |
| `evolution.tree.journal_identity:_positive` | `evolution.tree.execution.journal_identity:_positive` |  |
| `evolution.tree.journal_identity:_scorer_payload` | `evolution.tree.execution.journal_identity:_scorer_payload` |  |
| `evolution.tree.journal_identity:_sha` | `evolution.tree.execution.journal_identity:_sha` |  |
| `evolution.tree.journal_identity:_text` | `evolution.tree.execution.journal_identity:_text` |  |
| `evolution.tree.journal_identity:canonical_json` | `evolution.tree.execution.journal_identity:canonical_json` | |
| `evolution.tree.journal_identity:case_payload` | `evolution.tree.execution.journal_identity:case_payload` |  |
| `evolution.tree.journal_identity:checked_case_repeats` | `evolution.tree.execution.journal_identity:checked_case_repeats` |  |
| `evolution.tree.journal_identity:checked_case_subset` | `evolution.tree.execution.journal_identity:checked_case_subset` |  |
| `evolution.tree.journal_identity:corpus_digest` | `evolution.tree.execution.journal_identity:corpus_digest` | |
| `evolution.tree.journal_identity:effective_scorer_identity` | `evolution.tree.execution.journal_identity:effective_scorer_identity` |  |
| `evolution.tree.journal_identity:resolve_package_version` | `evolution.tree.execution.journal_identity:resolve_package_version` |  |
| `evolution.tree.journal_identity:scorer_binding` | `evolution.tree.execution.journal_identity:scorer_binding` | |
| `evolution.tree.journal_identity:scoring_input_digest` | `evolution.tree.execution.journal_identity:scoring_input_digest` |  |
| `evolution.tree.journal_projections:Projection` | `evolution.tree.execution.journal_projections:Projection` | |
| `evolution.tree.journal_projections:_COMMON` | `evolution.tree.execution.journal_projections:_COMMON` |  |
| `evolution.tree.journal_projections:_SAMPLE` | `evolution.tree.execution.journal_projections:_SAMPLE` |  |
| `evolution.tree.journal_projections:_disposition` | `evolution.tree.execution.journal_projections:_disposition` |  |
| `evolution.tree.journal_projections:_identifier` | `evolution.tree.execution.journal_projections:_identifier` |  |
| `evolution.tree.journal_projections:_integer` | `evolution.tree.execution.journal_projections:_integer` |  |
| `evolution.tree.journal_projections:_keys` | `evolution.tree.execution.journal_projections:_keys` |  |
| `evolution.tree.journal_projections:_project` | `evolution.tree.execution.journal_projections:_project` |  |
| `evolution.tree.journal_projections:_require` | `evolution.tree.execution.journal_projections:_require` |  |
| `evolution.tree.journal_projections:_sha` | `evolution.tree.execution.journal_projections:_sha` |  |
| `evolution.tree.journal_projections:_validate` | `evolution.tree.execution.journal_projections:_validate` |  |
| `evolution.tree.journal_projections:project_events` | `evolution.tree.execution.journal_projections:project_events` |  |
| `evolution.tree.journal_projections:project_result` | `evolution.tree.execution.journal_projections:project_result` |  |
| `evolution.tree.journal_projections:project_status` | `evolution.tree.execution.journal_projections:project_status` |  |
| `evolution.tree.journal_recovery:ExecutionResumeDecision` | `evolution.tree.execution.journal_recovery:ExecutionResumeDecision` |  |
| `evolution.tree.journal_recovery:OwnedCleanup` | `evolution.tree.execution.journal_recovery:OwnedCleanup` |  |
| `evolution.tree.journal_recovery:RecoveryAction` | `evolution.tree.execution.journal_recovery:RecoveryAction` |  |
| `evolution.tree.journal_recovery:RecoveryPlan` | `evolution.tree.execution.journal_recovery:RecoveryPlan` |  |
| `evolution.tree.journal_recovery:close_interrupted_operations` | `evolution.tree.execution.journal_recovery:close_interrupted_operations` |  |
| `evolution.tree.journal_recovery:recover_journal` | `evolution.tree.execution.journal_recovery:recover_journal` |  |
| `evolution.tree.journal_recovery:recovery_plan` | `evolution.tree.execution.journal_recovery:recovery_plan` |  |
| `evolution.tree.journal_rollout:ConnectionAdmission` | `evolution.tree.execution.journal_rollout:ConnectionAdmission` |  |
| `evolution.tree.journal_rollout:JournalRolloutAdapter` | `evolution.tree.execution.journal_rollout:JournalRolloutAdapter` |  |
| `evolution.tree.journal_rollout:NativeJournal` | `evolution.tree.execution.journal_rollout:NativeJournal` |  |
| `evolution.tree.journal_rollout:_BoundInstance` | `evolution.tree.execution.journal_rollout:_BoundInstance` |  |
| `evolution.tree.journal_rollout:_digest` | `evolution.tree.execution.journal_rollout:_digest` |  |
| `evolution.tree.journal_rollout:_failure` | `evolution.tree.execution.journal_rollout:_failure` |  |
| `evolution.tree.journal_rollout:durable_rollout` | `evolution.tree.execution.journal_rollout:durable_rollout` |  |
| `evolution.tree.journal_schema:CONNECTION_KEYS` | `evolution.tree.execution.journal_schema:CONNECTION_KEYS` |  |
| `evolution.tree.journal_schema:ERROR_CODE` | `evolution.tree.execution.journal_schema:ERROR_CODE` |  |
| `evolution.tree.journal_schema:EXTERNAL_OPERATIONS` | `evolution.tree.execution.journal_schema:EXTERNAL_OPERATIONS` |  |
| `evolution.tree.journal_schema:Envelope` | `evolution.tree.storage.envelope:Envelope` | |
| `evolution.tree.journal_schema:FAILURES` | `evolution.tree.execution.journal_schema:FAILURES` |  |
| `evolution.tree.journal_schema:Lifecycle` | `evolution.tree.execution.journal_schema:Lifecycle` |  |
| `evolution.tree.journal_schema:ROW_PROVENANCE_KEYS` | `evolution.tree.execution.journal_schema:ROW_PROVENANCE_KEYS` |  |
| `evolution.tree.journal_schema:S1_FAMILY` | `evolution.tree.execution.journal_schema:S1_FAMILY` |  |
| `evolution.tree.journal_schema:SAMPLE_FIELDS` | `evolution.tree.execution.journal_schema:SAMPLE_FIELDS` |  |
| `evolution.tree.journal_schema:SCHEMA_VERSION` | `evolution.tree.execution.journal_schema:SCHEMA_VERSION` |  |
| `evolution.tree.journal_schema:SHA256` | `evolution.tree.execution.journal_schema:SHA256` |  |
| `evolution.tree.journal_schema:_SHA256` | `evolution.tree.execution.journal_schema:_SHA256` |  |
| `evolution.tree.journal_schema:_validate_connection` | `evolution.tree.execution.journal_schema:_validate_connection` |  |
| `evolution.tree.journal_schema:_validate_payload` | `evolution.tree.execution.journal_schema:_validate_payload` |  |
| `evolution.tree.journal_schema:_validate_row_provenance` | `evolution.tree.execution.journal_schema:_validate_row_provenance` |  |
| `evolution.tree.journal_schema:components_digest` | `evolution.tree.execution.journal_schema:components_digest` |  |
| `evolution.tree.journal_schema:ordinal_limit` | `evolution.tree.execution.journal_schema:ordinal_limit` |  |
| `evolution.tree.journal_schema:try_is_retryable` | `evolution.tree.execution.journal_schema:try_is_retryable` | |
| `evolution.tree.journal_scoring:durable_score` | `evolution.tree.execution.journal_scoring:durable_score` |  |
| `evolution.tree.rollout:AdapterCapabilities` | `evolution.tree.execution.interaction:AdapterCapabilities` | |
| `evolution.tree.rollout:CallRetries` | `evolution.tree.execution.interaction:CallRetries` | |
| `evolution.tree.rollout:CandidateIntegrityError` | `evolution.tree.execution.interaction:CandidateIntegrityError` | |
| `evolution.tree.rollout:CandidateRefT` | `evolution.tree.execution.interaction:CandidateRefT` |  |
| `evolution.tree.rollout:CandidateReference` | `evolution.tree.execution.interaction:CandidateReference` |  |
| `evolution.tree.rollout:CandidateView` | `evolution.tree.execution.interaction:CandidateView` | |
| `evolution.tree.rollout:DescribingTargetInstance` | `evolution.tree.execution.interaction:DescribingTargetInstance` |  |
| `evolution.tree.rollout:Environment` | `evolution.tree.execution.interaction:Environment` | |
| `evolution.tree.rollout:EnvironmentIdentity` | `evolution.tree.execution.interaction:EnvironmentIdentity` |  |
| `evolution.tree.rollout:EnvironmentView` | `evolution.tree.execution.interaction:EnvironmentView` |  |
| `evolution.tree.rollout:EvalCase` | `evolution.tree.execution.interaction:EvalCase` | |
| `evolution.tree.rollout:History` | `evolution.tree.execution.interaction:History` |  |
| `evolution.tree.rollout:InstanceProvenance` | `evolution.tree.execution.interaction:InstanceProvenance` | |
| `evolution.tree.rollout:InstanceRecovery` | `evolution.tree.execution.interaction:InstanceRecovery` |  |
| `evolution.tree.rollout:MaterializationStrategy` | `evolution.tree.execution.interaction:MaterializationStrategy` |  |
| `evolution.tree.rollout:MaterializedCandidateT` | `evolution.tree.execution.interaction:MaterializedCandidateT` |  |
| `evolution.tree.rollout:Message` | `evolution.tree.execution.interaction:Message` |  |
| `evolution.tree.rollout:Provenance` | `evolution.tree.execution.interaction:Provenance` |  |
| `evolution.tree.rollout:ResumeDisposition` | `evolution.tree.execution.interaction:ResumeDisposition` |  |
| `evolution.tree.rollout:RolloutBudget` | `evolution.tree.execution.interaction:RolloutBudget` | |
| `evolution.tree.rollout:RolloutEnvironmentError` | `evolution.tree.execution.interaction:RolloutEnvironmentError` | |
| `evolution.tree.rollout:RolloutInfrastructureError` | `evolution.tree.execution.interaction:RolloutInfrastructureError` | |
| `evolution.tree.rollout:RolloutJournal` | `evolution.tree.execution.interaction:RolloutJournal` |  |
| `evolution.tree.rollout:RolloutKey` | `evolution.tree.execution.interaction:RolloutKey` |  |
| `evolution.tree.rollout:RolloutRecoveryError` | `evolution.tree.execution.interaction:RolloutRecoveryError` |  |
| `evolution.tree.rollout:RolloutSample` | `evolution.tree.execution.interaction:RolloutSample` | |
| `evolution.tree.rollout:RolloutTargetUnstable` | `evolution.tree.execution.interaction:RolloutTargetUnstable` | |
| `evolution.tree.rollout:RolloutTimeBudgetExceeded` | `evolution.tree.execution.interaction:RolloutTimeBudgetExceeded` | |
| `evolution.tree.rollout:SampleIdentity` | `evolution.tree.execution.interaction:SampleIdentity` |  |
| `evolution.tree.rollout:ScoreResult` | `evolution.tree.execution.interaction:ScoreResult` | |
| `evolution.tree.rollout:ScoreResultError` | `evolution.tree.execution.interaction:ScoreResultError` |  |
| `evolution.tree.rollout:Scorer` | `evolution.tree.execution.interaction:Scorer` | |
| `evolution.tree.rollout:ScorerIdentity` | `evolution.tree.execution.interaction:ScorerIdentity` | |
| `evolution.tree.rollout:ScoringError` | `evolution.tree.execution.interaction:ScoringError` |  |
| `evolution.tree.rollout:ScoringView` | `evolution.tree.execution.interaction:ScoringView` |  |
| `evolution.tree.rollout:ScriptedEnvironment` | `evolution.tree.execution.interaction:ScriptedEnvironment` | |
| `evolution.tree.rollout:SeedMode` | `evolution.tree.execution.interaction:SeedMode` |  |
| `evolution.tree.rollout:SemanticIdentity` | `evolution.tree.execution.interaction:SemanticIdentity` | |
| `evolution.tree.rollout:StaticInputEnvironment` | `evolution.tree.execution.interaction:StaticInputEnvironment` | |
| `evolution.tree.rollout:TargetAdapter` | `evolution.tree.execution.interaction:TargetAdapter` |  |
| `evolution.tree.rollout:TargetIdentity` | `evolution.tree.execution.interaction:TargetIdentity` | |
| `evolution.tree.rollout:TargetInstance` | `evolution.tree.execution.interaction:TargetInstance` | |
| `evolution.tree.rollout:TargetSession` | `evolution.tree.execution.interaction:TargetSession` |  |
| `evolution.tree.rollout:TracingScorer` | `evolution.tree.execution.interaction:TracingScorer` |  |
| `evolution.tree.rollout:Trajectory` | `evolution.tree.execution.interaction:Trajectory` |  |
| `evolution.tree.rollout:Turn` | `evolution.tree.execution.interaction:Turn` |  |
| `evolution.tree.rollout:TurnResult` | `evolution.tree.execution.interaction:TurnResult` | |
| `evolution.tree.rollout:Usage` | `evolution.tree.execution.usage:Usage` | |
| `evolution.tree.rollout:_INSTANCE_FIELDS` | `evolution.tree.execution.interaction:_INSTANCE_FIELDS` |  |
| `evolution.tree.rollout:_JITTER` | `evolution.tree.execution.rollout:_JITTER` |  |
| `evolution.tree.rollout:_ProvenanceUnion` | `evolution.tree.execution.rollout:_ProvenanceUnion` |  |
| `evolution.tree.rollout:_RolloutDeadlineExceeded` | `evolution.tree.execution.rollout:_RolloutDeadlineExceeded` |  |
| `evolution.tree.rollout:_SEMANTIC_FIELDS` | `evolution.tree.execution.interaction:_SEMANTIC_FIELDS` |  |
| `evolution.tree.rollout:_SLEEP` | `evolution.tree.execution.rollout:_SLEEP` |  |
| `evolution.tree.rollout:_T` | `evolution.tree.execution.rollout:_T` |  |
| `evolution.tree.rollout:_accepts_keyword` | `evolution.tree.execution.rollout:_accepts_keyword` |  |
| `evolution.tree.rollout:_attempt_evidence` | `evolution.tree.execution.rollout:_attempt_evidence` |  |
| `evolution.tree.rollout:_attempt_loop` | `evolution.tree.execution.rollout:_attempt_loop` |  |
| `evolution.tree.rollout:_call_attempts` | `evolution.tree.execution.rollout:_call_attempts` |  |
| `evolution.tree.rollout:_canonical_list` | `evolution.tree.execution.rollout:_canonical_list` |  |
| `evolution.tree.rollout:_canonical_string` | `evolution.tree.execution.rollout:_canonical_string` |  |
| `evolution.tree.rollout:_close_session` | `evolution.tree.execution.rollout:_close_session` |  |
| `evolution.tree.rollout:_code_of` | `evolution.tree.execution.rollout:_code_of` |  |
| `evolution.tree.rollout:_core_check` | `evolution.tree.execution.rollout:_core_check` |  |
| `evolution.tree.rollout:_effective_max_turns` | `evolution.tree.execution.rollout:_effective_max_turns` |  |
| `evolution.tree.rollout:_elapsed_ms` | `evolution.tree.execution.rollout:_elapsed_ms` |  |
| `evolution.tree.rollout:_exact_keys` | `evolution.tree.execution.interaction:_exact_keys` |  |
| `evolution.tree.rollout:_external_attempt` | `evolution.tree.execution.rollout:_external_attempt` |  |
| `evolution.tree.rollout:_external_call` | `evolution.tree.execution.rollout:_external_call` |  |
| `evolution.tree.rollout:_failure_evidence` | `evolution.tree.execution.rollout:_failure_evidence` |  |
| `evolution.tree.rollout:_freeze` | `evolution.tree.execution.interaction:_freeze` |  |
| `evolution.tree.rollout:_frozen_mapping` | `evolution.tree.execution.interaction:_frozen_mapping` |  |
| `evolution.tree.rollout:_instances_requiring_release` | `evolution.tree.execution.rollout:_instances_requiring_release` |  |
| `evolution.tree.rollout:_json_digest` | `evolution.tree.execution.interaction:_json_digest` |  |
| `evolution.tree.rollout:_may_resend` | `evolution.tree.execution.rollout:_may_resend` |  |
| `evolution.tree.rollout:_payload` | `evolution.tree.execution.interaction:_payload` |  |
| `evolution.tree.rollout:_request_id` | `evolution.tree.execution.rollout:_request_id` |  |
| `evolution.tree.rollout:_require_nonblank` | `evolution.tree.execution.interaction:_require_nonblank` |  |
| `evolution.tree.rollout:_require_stable_identity` | `evolution.tree.execution.rollout:_require_stable_identity` |  |
| `evolution.tree.rollout:_session_event_metadata` | `evolution.tree.execution.rollout:_session_event_metadata` |  |
| `evolution.tree.rollout:_target_call_failure` | `evolution.tree.execution.rollout:_target_call_failure` |  |
| `evolution.tree.rollout:_turn_result_summary` | `evolution.tree.execution.rollout:_turn_result_summary` |  |
| `evolution.tree.rollout:_validate_optional_seed` | `evolution.tree.execution.interaction:_validate_optional_seed` |  |
| `evolution.tree.rollout:_validate_session_for_sample` | `evolution.tree.execution.rollout:_validate_session_for_sample` |  |
| `evolution.tree.rollout:_validate_target_for_sample` | `evolution.tree.execution.rollout:_validate_target_for_sample` |  |
| `evolution.tree.rollout:are_samples_paired` | `evolution.tree.execution.interaction:are_samples_paired` |  |
| `evolution.tree.rollout:backoff_delay` | `evolution.tree.execution.rollout:backoff_delay` |  |
| `evolution.tree.rollout:classify_rollout_resume` | `evolution.tree.execution.rollout:classify_rollout_resume` |  |
| `evolution.tree.rollout:condition_key` | `evolution.tree.execution.interaction:condition_key` |  |
| `evolution.tree.rollout:materialize_target` | `evolution.tree.execution.rollout:materialize_target` |  |
| `evolution.tree.rollout:recover_target_instances` | `evolution.tree.execution.rollout:recover_target_instances` |  |
| `evolution.tree.rollout:release_target` | `evolution.tree.execution.rollout:release_target` |  |
| `evolution.tree.rollout:rollout` | `evolution.tree.execution.rollout:rollout` |  |
| `evolution.tree.rollout:validate_score_result` | `evolution.tree.execution.interaction:validate_score_result` |  |
| `evolution.tree.rollout_scoring:CRITERION` | `evolution.tree.execution.rollout_scoring:CRITERION` |  |
| `evolution.tree.rollout_scoring:MAX_DIAGNOSTICS_BYTES` | `evolution.tree.execution.rollout_scoring:MAX_DIAGNOSTICS_BYTES` |  |
| `evolution.tree.rollout_scoring:MAX_DIAGNOSTICS_DEPTH` | `evolution.tree.execution.rollout_scoring:MAX_DIAGNOSTICS_DEPTH` |  |
| `evolution.tree.rollout_scoring:MAX_VERDICTS` | `evolution.tree.execution.rollout_scoring:MAX_VERDICTS` |  |
| `evolution.tree.rollout_scoring:MAX_VERDICT_NOTE_CHARS` | `evolution.tree.execution.rollout_scoring:MAX_VERDICT_NOTE_CHARS` |  |
| `evolution.tree.rollout_scoring:ScoringError` | `evolution.tree.execution.interaction:ScoringError` | |
| `evolution.tree.rollout_scoring:SubprocessScorer` | `evolution.tree.execution.rollout_scoring:SubprocessScorer` | |
| `evolution.tree.rollout_scoring:_DuplicateJsonField` | `evolution.tree.execution.rollout_scoring:_DuplicateJsonField` |  |
| `evolution.tree.rollout_scoring:_TRACE_ID` | `evolution.tree.execution.rollout_scoring:_TRACE_ID` |  |
| `evolution.tree.rollout_scoring:_VERDICT_KEYS` | `evolution.tree.execution.rollout_scoring:_VERDICT_KEYS` |  |
| `evolution.tree.rollout_scoring:_VERDICT_REQUIRED` | `evolution.tree.execution.rollout_scoring:_VERDICT_REQUIRED` |  |
| `evolution.tree.rollout_scoring:_close_scorer_traces` | `evolution.tree.execution.rollout_scoring:_close_scorer_traces` |  |
| `evolution.tree.rollout_scoring:_diagnostics_too_deep` | `evolution.tree.execution.rollout_scoring:_diagnostics_too_deep` |  |
| `evolution.tree.rollout_scoring:_json_digest` | `evolution.tree.execution.rollout_scoring:_json_digest` |  |
| `evolution.tree.rollout_scoring:_json_value` | `evolution.tree.execution.rollout_scoring:_json_value` |  |
| `evolution.tree.rollout_scoring:_object_without_duplicate_fields` | `evolution.tree.execution.rollout_scoring:_object_without_duplicate_fields` |  |
| `evolution.tree.rollout_scoring:_parse_json_object` | `evolution.tree.execution.rollout_scoring:_parse_json_object` |  |
| `evolution.tree.rollout_scoring:_parse_score_result` | `evolution.tree.execution.rollout_scoring:_parse_score_result` |  |
| `evolution.tree.rollout_scoring:_reject_json_constant` | `evolution.tree.execution.rollout_scoring:_reject_json_constant` |  |
| `evolution.tree.rollout_scoring:parse_diagnostics` | `evolution.tree.execution.rollout_scoring:parse_diagnostics` |  |
| `evolution.tree.rollout_scoring:parse_scorer_stdout` | `evolution.tree.execution.rollout_scoring:parse_scorer_stdout` |  |
| `evolution.tree.rollout_scoring:parse_verdicts` | `evolution.tree.execution.rollout_scoring:parse_verdicts` |  |
| `evolution.tree.sample_outcomes:SAMPLE_REASON_REGISTRY_V1` | `evolution.tree.execution.sample_outcomes:SAMPLE_REASON_REGISTRY_V1` |  |
| `evolution.tree.sample_outcomes:SAMPLE_REASON_SCHEMA_VERSION` | `evolution.tree.execution.sample_outcomes:SAMPLE_REASON_SCHEMA_VERSION` |  |
| `evolution.tree.sample_outcomes:SCORED_TERMINALS` | `evolution.tree.execution.sample_outcomes:SCORED_TERMINALS` |  |
| `evolution.tree.sample_outcomes:SampleDisposition` | `evolution.tree.execution.sample_outcomes:SampleDisposition` |  |
| `evolution.tree.sample_outcomes:SampleDispositionKind` | `evolution.tree.execution.sample_outcomes:SampleDispositionKind` |  |
| `evolution.tree.sample_outcomes:SampleOutcome` | `evolution.tree.execution.sample_outcomes:SampleOutcome` |  |
| `evolution.tree.sample_outcomes:SampleOwner` | `evolution.tree.execution.sample_outcomes:SampleOwner` |  |
| `evolution.tree.sample_outcomes:SampleReasonId` | `evolution.tree.execution.sample_outcomes:SampleReasonId` |  |
| `evolution.tree.sample_outcomes:Terminal` | `evolution.tree.execution.sample_outcomes:Terminal` |  |
| `evolution.tree.sample_outcomes:_DispositionTuple` | `evolution.tree.execution.sample_outcomes:_DispositionTuple` |  |
| `evolution.tree.sample_outcomes:_EXPECTED_DISPOSITIONS` | `evolution.tree.execution.sample_outcomes:_EXPECTED_DISPOSITIONS` |  |
| `evolution.tree.sample_outcomes:_reason_registry` | `evolution.tree.execution.sample_outcomes:_reason_registry` |  |
| `evolution.tree.sample_outcomes:classify_sample_outcome` | `evolution.tree.execution.sample_outcomes:classify_sample_outcome` |  |
| `evolution.tree.sample_outcomes:disposition_for` | `evolution.tree.execution.sample_outcomes:disposition_for` | |
| `evolution.tree.sample_outcomes:sample_reason_registry_payload` | `evolution.tree.execution.sample_outcomes:sample_reason_registry_payload` |  |
| `evolution.tree.scorer_trace_writer:PackedRecord` | `evolution.tree.execution.scorer_trace_writer:PackedRecord` |  |
| `evolution.tree.scorer_trace_writer:PackedTraceWriter` | `evolution.tree.execution.scorer_trace_writer:PackedTraceWriter` |  |
| `evolution.tree.scorer_trace_writer:_CHUNK` | `evolution.tree.execution.scorer_trace_writer:_CHUNK` |  |
| `evolution.tree.scorer_trace_writer:_Session` | `evolution.tree.execution.scorer_trace_writer:_Session` |  |
| `evolution.tree.scorer_trace_writer:_chunks` | `evolution.tree.execution.scorer_trace_writer:_chunks` |  |
| `evolution.tree.scorer_trace_writer:_describe` | `evolution.tree.execution.scorer_trace_writer:_describe` |  |
| `evolution.tree.scorer_trace_writer:_fsync` | `evolution.tree.execution.scorer_trace_writer:_fsync` |  |
| `evolution.tree.scorer_trace_writer:_identity` | `evolution.tree.execution.scorer_trace_writer:_identity` |  |
| `evolution.tree.scorer_trace_writer:_open` | `evolution.tree.execution.scorer_trace_writer:_open` |  |
| `evolution.tree.scorer_trace_writer:_release` | `evolution.tree.execution.scorer_trace_writer:_release` |  |
| `evolution.tree.scorer_trace_writer:_sessions` | `evolution.tree.execution.scorer_trace_writer:_sessions` |  |
| `evolution.tree.scorer_trace_writer:_sessions_lock` | `evolution.tree.execution.scorer_trace_writer:_sessions_lock` |  |
| `evolution.tree.scorer_trace_writer:_truncate` | `evolution.tree.execution.scorer_trace_writer:_truncate` |  |
| `evolution.tree.scorer_trace_writer:_write` | `evolution.tree.execution.scorer_trace_writer:_write` |  |
| `evolution.tree.scorer_trace_writer:_write_all` | `evolution.tree.execution.scorer_trace_writer:_write_all` |  |
| `evolution.tree.scorer_traces:CONNECTION_LOGS` | `evolution.tree.execution.scorer_traces:CONNECTION_LOGS` |  |
| `evolution.tree.scorer_traces:MAX_HEADER_BYTES` | `evolution.tree.execution.scorer_traces:MAX_HEADER_BYTES` |  |
| `evolution.tree.scorer_traces:PACK_FORMAT` | `evolution.tree.execution.scorer_traces:PACK_FORMAT` | |
| `evolution.tree.scorer_traces:PACK_ID` | `evolution.tree.execution.scorer_traces:PACK_ID` |  |
| `evolution.tree.scorer_traces:SECTIONS` | `evolution.tree.execution.scorer_traces:SECTIONS` |  |
| `evolution.tree.scorer_traces:ScorerTraceReader` | `evolution.tree.execution.scorer_traces:ScorerTraceReader` |  |
| `evolution.tree.scorer_traces:ScorerTraceRecord` | `evolution.tree.execution.scorer_traces:ScorerTraceRecord` |  |
| `evolution.tree.scorer_traces:ScorerTraceStorageFault` | `evolution.tree.execution.scorer_traces:ScorerTraceStorageFault` |  |
| `evolution.tree.scorer_traces:ScorerTraceUnreadable` | `evolution.tree.execution.scorer_traces:ScorerTraceUnreadable` |  |
| `evolution.tree.scorer_traces:Section` | `evolution.tree.execution.scorer_traces:Section` |  |
| `evolution.tree.scorer_traces:TRACE_ID` | `evolution.tree.execution.scorer_traces:TRACE_ID` |  |
| `evolution.tree.scorer_traces:TRACE_ROOT` | `evolution.tree.execution.scorer_traces:TRACE_ROOT` |  |
| `evolution.tree.scorer_traces:TRACE_SUBDIR` | `evolution.tree.execution.scorer_traces:TRACE_SUBDIR` |  |
| `evolution.tree.scorer_traces:TraceStatus` | `evolution.tree.execution.scorer_traces:TraceStatus` |  |
| `evolution.tree.scorer_traces:TraceStream` | `evolution.tree.execution.scorer_traces:TraceStream` |  |
| `evolution.tree.scorer_traces:UnreadableCode` | `evolution.tree.execution.scorer_traces:UnreadableCode` |  |
| `evolution.tree.scorer_traces:_AdmittedDirectory` | `evolution.tree.execution.scorer_traces:_AdmittedDirectory` |  |
| `evolution.tree.scorer_traces:_CODES` | `evolution.tree.execution.scorer_traces:_CODES` |  |
| `evolution.tree.scorer_traces:_DIGEST` | `evolution.tree.execution.scorer_traces:_DIGEST` |  |
| `evolution.tree.scorer_traces:_DIRECTORY_FILES` | `evolution.tree.execution.scorer_traces:_DIRECTORY_FILES` |  |
| `evolution.tree.scorer_traces:_Entry` | `evolution.tree.execution.scorer_traces:_Entry` |  |
| `evolution.tree.scorer_traces:_KEYS` | `evolution.tree.execution.scorer_traces:_KEYS` |  |
| `evolution.tree.scorer_traces:_Location` | `evolution.tree.execution.scorer_traces:_Location` |  |
| `evolution.tree.scorer_traces:_Output` | `evolution.tree.execution.scorer_traces:_Output` |  |
| `evolution.tree.scorer_traces:_PackIndex` | `evolution.tree.execution.scorer_traces:_PackIndex` |  |
| `evolution.tree.scorer_traces:_Probed` | `evolution.tree.execution.scorer_traces:_Probed` |  |
| `evolution.tree.scorer_traces:_SECTION_NAMES` | `evolution.tree.execution.scorer_traces:_SECTION_NAMES` |  |
| `evolution.tree.scorer_traces:_SEGMENT` | `evolution.tree.execution.scorer_traces:_SEGMENT` |  |
| `evolution.tree.scorer_traces:_STATUSES` | `evolution.tree.execution.scorer_traces:_STATUSES` |  |
| `evolution.tree.scorer_traces:__all__` | `evolution.tree.execution.scorer_traces:__all__` |  |
| `evolution.tree.scorer_traces:_canonical` | `evolution.tree.execution.scorer_traces:_canonical` |  |
| `evolution.tree.scorer_traces:_count` | `evolution.tree.execution.scorer_traces:_count` |  |
| `evolution.tree.scorer_traces:_directory_result` | `evolution.tree.execution.scorer_traces:_directory_result` |  |
| `evolution.tree.scorer_traces:_encode_header` | `evolution.tree.execution.scorer_traces:_encode_header` |  |
| `evolution.tree.scorer_traces:_fsync` | `evolution.tree.execution.scorer_traces:_fsync` |  |
| `evolution.tree.scorer_traces:_index_file` | `evolution.tree.execution.scorer_traces:_index_file` |  |
| `evolution.tree.scorer_traces:_name_ids` | `evolution.tree.execution.scorer_traces:_name_ids` |  |
| `evolution.tree.scorer_traces:_nofollow_root` | `evolution.tree.execution.scorer_traces:_nofollow_root` |  |
| `evolution.tree.scorer_traces:_pread` | `evolution.tree.execution.scorer_traces:_pread` |  |
| `evolution.tree.scorer_traces:_probe_file` | `evolution.tree.execution.scorer_traces:_probe_file` |  |
| `evolution.tree.scorer_traces:_raw_close` | `evolution.tree.execution.scorer_traces:_raw_close` |  |
| `evolution.tree.scorer_traces:_raw_write` | `evolution.tree.execution.scorer_traces:_raw_write` |  |
| `evolution.tree.scorer_traces:_read_bounded` | `evolution.tree.execution.scorer_traces:_read_bounded` |  |
| `evolution.tree.scorer_traces:_scan` | `evolution.tree.execution.scorer_traces:_scan` |  |
| `evolution.tree.scorer_traces:_validate_limit` | `evolution.tree.execution.scorer_traces:_validate_limit` |  |
| `evolution.tree.scorer_traces:_verified_header` | `evolution.tree.execution.scorer_traces:_verified_header` |  |
| `evolution.tree.scorer_traces:_wanted` | `evolution.tree.execution.scorer_traces:_wanted` |  |
| `evolution.tree.scorer_traces:_well_formed` | `evolution.tree.execution.scorer_traces:_well_formed` |  |
| `evolution.tree.scorer_traces:close_trace_streams` | `evolution.tree.execution.scorer_traces:close_trace_streams` |  |
| `evolution.tree.scorer_traces:decode_trace_text` | `evolution.tree.execution.scorer_traces:decode_trace_text` |  |
| `evolution.tree.scorer_traces:drop_empty_streams` | `evolution.tree.execution.scorer_traces:drop_empty_streams` |  |
| `evolution.tree.scorer_traces:encode_pack_record` | `evolution.tree.execution.scorer_traces:encode_pack_record` | |
| `evolution.tree.scorer_traces:open_trace_stream` | `evolution.tree.execution.scorer_traces:open_trace_stream` |  |
| `evolution.tree.scorer_traces:read_scorer_trace` | `evolution.tree.execution.scorer_traces:read_scorer_trace` |  |
| `evolution.tree.scorer_traces:settle_trace_streams` | `evolution.tree.execution.scorer_traces:settle_trace_streams` |  |
| `evolution.tree.scorer_traces:trace_location` | `evolution.tree.execution.scorer_traces:trace_location` |  |
| `evolution.tree.scorer_traces:trace_storage_fault` | `evolution.tree.execution.scorer_traces:trace_storage_fault` |  |
| `evolution.tree.wire_v2:Capabilities` | `evolution.tree.execution.wire_v2:Capabilities` |  |
| `evolution.tree.wire_v2:ERROR_CODES` | `evolution.tree.execution.wire_v2:ERROR_CODES` |  |
| `evolution.tree.wire_v2:Health` | `evolution.tree.execution.wire_v2:Health` |  |
| `evolution.tree.wire_v2:IDENTIFIER` | `evolution.tree.execution.wire_v2:IDENTIFIER` |  |
| `evolution.tree.wire_v2:InstanceIdentity` | `evolution.tree.execution.wire_v2:InstanceIdentity` |  |
| `evolution.tree.wire_v2:InstanceReleased` | `evolution.tree.execution.wire_v2:InstanceReleased` |  |
| `evolution.tree.wire_v2:MAX_RUN_ID` | `evolution.tree.execution.wire_v2:MAX_RUN_ID` |  |
| `evolution.tree.wire_v2:PREFIX` | `evolution.tree.execution.wire_v2:PREFIX` | |
| `evolution.tree.wire_v2:PROTOCOL_VERSION` | `evolution.tree.execution.wire_v2:PROTOCOL_VERSION` |  |
| `evolution.tree.wire_v2:PROVENANCE_MAX_BYTES` | `evolution.tree.execution.wire_v2:PROVENANCE_MAX_BYTES` |  |
| `evolution.tree.wire_v2:PROVENANCE_MAX_USED` | `evolution.tree.execution.wire_v2:PROVENANCE_MAX_USED` |  |
| `evolution.tree.wire_v2:REQUIRED_ATTESTS` | `evolution.tree.execution.wire_v2:REQUIRED_ATTESTS` | |
| `evolution.tree.wire_v2:SEED_MODES` | `evolution.tree.execution.wire_v2:SEED_MODES` |  |
| `evolution.tree.wire_v2:STRATEGY` | `evolution.tree.execution.wire_v2:STRATEGY` | |
| `evolution.tree.wire_v2:SessionClosed` | `evolution.tree.execution.wire_v2:SessionClosed` |  |
| `evolution.tree.wire_v2:SessionCreated` | `evolution.tree.execution.wire_v2:SessionCreated` |  |
| `evolution.tree.wire_v2:TurnResponse` | `evolution.tree.execution.wire_v2:TurnResponse` |  |
| `evolution.tree.wire_v2:WireError` | `evolution.tree.execution.wire_v2:WireError` |  |
| `evolution.tree.wire_v2:_COMPONENTS_MAX` | `evolution.tree.execution.wire_v2:_COMPONENTS_MAX` |  |
| `evolution.tree.wire_v2:_COMPONENTS_MAX_BYTES` | `evolution.tree.execution.wire_v2:_COMPONENTS_MAX_BYTES` |  |
| `evolution.tree.wire_v2:_COMPONENT_ID` | `evolution.tree.execution.wire_v2:_COMPONENT_ID` |  |
| `evolution.tree.wire_v2:_DESCRIPTION_MAX_BYTES` | `evolution.tree.execution.wire_v2:_DESCRIPTION_MAX_BYTES` |  |
| `evolution.tree.wire_v2:_DESCRIPTION_MAX_KEYS` | `evolution.tree.execution.wire_v2:_DESCRIPTION_MAX_KEYS` |  |
| `evolution.tree.wire_v2:_INSTANCE_ID` | `evolution.tree.execution.wire_v2:_INSTANCE_ID` |  |
| `evolution.tree.wire_v2:_MAX_RUN_ID` | `evolution.tree.execution.wire_v2:_MAX_RUN_ID` |  |
| `evolution.tree.wire_v2:_NAME_MAX` | `evolution.tree.execution.wire_v2:_NAME_MAX` |  |
| `evolution.tree.wire_v2:_PATH_MAX` | `evolution.tree.execution.wire_v2:_PATH_MAX` |  |
| `evolution.tree.wire_v2:_PROVENANCE_ID_MAX` | `evolution.tree.execution.wire_v2:_PROVENANCE_ID_MAX` |  |
| `evolution.tree.wire_v2:_REASON_ID` | `evolution.tree.execution.wire_v2:_REASON_ID` |  |
| `evolution.tree.wire_v2:_REASON_KEYS` | `evolution.tree.execution.wire_v2:_REASON_KEYS` |  |
| `evolution.tree.wire_v2:_SHA256` | `evolution.tree.execution.wire_v2:_SHA256` |  |
| `evolution.tree.wire_v2:_artifact_path` | `evolution.tree.execution.wire_v2:_artifact_path` |  |
| `evolution.tree.wire_v2:_bool` | `evolution.tree.execution.wire_v2:_bool` |  |
| `evolution.tree.wire_v2:_identifier` | `evolution.tree.execution.wire_v2:_identifier` |  |
| `evolution.tree.wire_v2:_identifier_tuple` | `evolution.tree.execution.wire_v2:_identifier_tuple` |  |
| `evolution.tree.wire_v2:_int` | `evolution.tree.execution.wire_v2:_int` |  |
| `evolution.tree.wire_v2:_keys` | `evolution.tree.execution.wire_v2:_keys` |  |
| `evolution.tree.wire_v2:_parse_capabilities` | `evolution.tree.execution.wire_v2:_parse_capabilities` |  |
| `evolution.tree.wire_v2:_parse_usage` | `evolution.tree.execution.wire_v2:_parse_usage` |  |
| `evolution.tree.wire_v2:_require` | `evolution.tree.execution.wire_v2:_require` |  |
| `evolution.tree.wire_v2:_sha` | `evolution.tree.execution.wire_v2:_sha` |  |
| `evolution.tree.wire_v2:_str_list` | `evolution.tree.execution.wire_v2:_str_list` |  |
| `evolution.tree.wire_v2:canonical_body` | `evolution.tree.execution.wire_v2:canonical_body` | |
| `evolution.tree.wire_v2:is_artifact_path` | `evolution.tree.execution.wire_v2:is_artifact_path` |  |
| `evolution.tree.wire_v2:is_instance_id` | `evolution.tree.execution.wire_v2:is_instance_id` | |
| `evolution.tree.wire_v2:new_instance_id` | `evolution.tree.execution.wire_v2:new_instance_id` | |
| `evolution.tree.wire_v2:parse_close` | `evolution.tree.execution.wire_v2:parse_close` |  |
| `evolution.tree.wire_v2:parse_error` | `evolution.tree.execution.wire_v2:parse_error` | |
| `evolution.tree.wire_v2:parse_health` | `evolution.tree.execution.wire_v2:parse_health` | |
| `evolution.tree.wire_v2:parse_instance` | `evolution.tree.execution.wire_v2:parse_instance` |  |
| `evolution.tree.wire_v2:parse_release` | `evolution.tree.execution.wire_v2:parse_release` | |
| `evolution.tree.wire_v2:parse_session` | `evolution.tree.execution.wire_v2:parse_session` |  |
| `evolution.tree.wire_v2:parse_turn` | `evolution.tree.execution.wire_v2:parse_turn` |  |
| `evolution.tree.wire_v2:validate_instance_components` | `evolution.tree.execution.wire_v2:validate_instance_components` |  |
| `evolution.tree.wire_v2:validate_instance_description` | `evolution.tree.execution.wire_v2:validate_instance_description` | |
| `evolution.tree.wire_v2:validate_turn_provenance` | `evolution.tree.execution.wire_v2:validate_turn_provenance` |  |
| `evolution.tree.wire_v2:validate_turn_reason` | `evolution.tree.execution.wire_v2:validate_turn_reason` | |

#### Artifacts import migration

| Old import | New import | |
|---|---|---|
| `evolution.tree.artifact_copy:ArtifactPolicyV1` | `evolution.tree.artifacts.artifact_policy:ArtifactPolicyV1` |  |
| `evolution.tree.artifact_copy:ArtifactUnsafeError` | `evolution.tree.artifacts.artifact_errors:ArtifactUnsafeError` |  |
| `evolution.tree.artifact_copy:Entry` | `evolution.tree.artifacts.artifact_policy:Entry` |  |
| `evolution.tree.artifact_copy:StagedCopy` | `evolution.tree.artifacts.artifact_copy:StagedCopy` |  |
| `evolution.tree.artifact_copy:TreeScan` | `evolution.tree.artifacts.artifact_policy:TreeScan` |  |
| `evolution.tree.artifact_copy:_NO_FOLLOW` | `evolution.tree.artifacts.artifact_copy:_NO_FOLLOW` |  |
| `evolution.tree.artifact_copy:_WRITE_BITS` | `evolution.tree.artifacts.artifact_copy:_WRITE_BITS` |  |
| `evolution.tree.artifact_copy:_clear_obstruction` | `evolution.tree.artifacts.artifact_copy:_clear_obstruction` |  |
| `evolution.tree.artifact_copy:_clear_root_write_bits` | `evolution.tree.artifacts.artifact_copy:_clear_root_write_bits` |  |
| `evolution.tree.artifact_copy:_copy_and_verify` | `evolution.tree.artifacts.artifact_copy:_copy_and_verify` |  |
| `evolution.tree.artifact_copy:_copy_entries` | `evolution.tree.artifacts.artifact_copy:_copy_entries` |  |
| `evolution.tree.artifact_copy:_fsync_path` | `evolution.tree.artifacts.artifact_copy:_fsync_path` |  |
| `evolution.tree.artifact_copy:_same_tree` | `evolution.tree.artifacts.artifact_copy:_same_tree` |  |
| `evolution.tree.artifact_copy:_set_frozen` | `evolution.tree.artifacts.artifact_copy:_set_frozen` |  |
| `evolution.tree.artifact_copy:check_destination_parent` | `evolution.tree.artifacts.artifact_copy:check_destination_parent` |  |
| `evolution.tree.artifact_copy:entry_set` | `evolution.tree.artifacts.artifact_policy:entry_set` |  |
| `evolution.tree.artifact_copy:fsync_tree` | `evolution.tree.artifacts.artifact_copy:fsync_tree` |  |
| `evolution.tree.artifact_copy:map_os_error` | `evolution.tree.artifacts.artifact_copy:map_os_error` |  |
| `evolution.tree.artifact_copy:publish` | `evolution.tree.artifacts.artifact_copy:publish` |  |
| `evolution.tree.artifact_copy:remove_tree` | `evolution.tree.artifacts.artifact_copy:remove_tree` |  |
| `evolution.tree.artifact_copy:reraise_walk_error` | `evolution.tree.artifacts.artifact_policy:reraise_walk_error` |  |
| `evolution.tree.artifact_copy:restore_writable` | `evolution.tree.artifacts.artifact_copy:restore_writable` |  |
| `evolution.tree.artifact_copy:scan_tree` | `evolution.tree.artifacts.artifact_policy:scan_tree` |  |
| `evolution.tree.artifact_copy:snapshot_tree` | `evolution.tree.artifacts.artifact_copy:snapshot_tree` |  |
| `evolution.tree.artifact_copy:stage_copy` | `evolution.tree.artifacts.artifact_copy:stage_copy` |  |
| `evolution.tree.artifact_errors:ArtifactAdmissionRefused` | `evolution.tree.artifacts.artifact_errors:ArtifactAdmissionRefused` |  |
| `evolution.tree.artifact_errors:ArtifactCapacityError` | `evolution.tree.artifacts.artifact_errors:ArtifactCapacityError` |  |
| `evolution.tree.artifact_errors:ArtifactCorruptionError` | `evolution.tree.artifacts.artifact_errors:ArtifactCorruptionError` |  |
| `evolution.tree.artifact_errors:ArtifactError` | `evolution.tree.execution.candidate_integrity:ArtifactError` |  |
| `evolution.tree.artifact_errors:ArtifactStoreStopped` | `evolution.tree.artifacts.artifact_errors:ArtifactStoreStopped` |  |
| `evolution.tree.artifact_errors:ArtifactUnsafeError` | `evolution.tree.artifacts.artifact_errors:ArtifactUnsafeError` |  |
| `evolution.tree.artifact_ledger:ARTIFACTS_DIR` | `evolution.tree.artifacts.artifact_ledger:ARTIFACTS_DIR` |  |
| `evolution.tree.artifact_ledger:ARTIFACT_FAMILY` | `evolution.tree.artifacts.artifact_schema:ARTIFACT_FAMILY` |  |
| `evolution.tree.artifact_ledger:AppendLog` | `evolution.tree.storage.append_log:AppendLog` |  |
| `evolution.tree.artifact_ledger:ArtifactCorruptionError` | `evolution.tree.artifacts.artifact_errors:ArtifactCorruptionError` |  |
| `evolution.tree.artifact_ledger:ArtifactLedger` | `evolution.tree.artifacts.artifact_ledger:ArtifactLedger` |  |
| `evolution.tree.artifact_ledger:ArtifactLifecycle` | `evolution.tree.artifacts.artifact_schema:ArtifactLifecycle` |  |
| `evolution.tree.artifact_ledger:ArtifactPolicyV1` | `evolution.tree.artifacts.artifact_policy:ArtifactPolicyV1` |  |
| `evolution.tree.artifact_ledger:ArtifactStoreIdentityV1` | `evolution.tree.artifacts.artifact_schema:ArtifactStoreIdentityV1` |  |
| `evolution.tree.artifact_ledger:ArtifactStoreStopped` | `evolution.tree.artifacts.artifact_errors:ArtifactStoreStopped` |  |
| `evolution.tree.artifact_ledger:Envelope` | `evolution.tree.storage.envelope:Envelope` |  |
| `evolution.tree.artifact_ledger:FOREIGN_LAYOUT` | `evolution.tree.artifacts.artifact_ledger:FOREIGN_LAYOUT` |  |
| `evolution.tree.artifact_ledger:IdentityError` | `evolution.tree.execution.journal_identity:IdentityError` |  |
| `evolution.tree.artifact_ledger:JournalAdmissionRefused` | `evolution.tree.storage.journal_errors:JournalAdmissionRefused` |  |
| `evolution.tree.artifact_ledger:JournalFault` | `evolution.tree.storage.journal_errors:JournalFault` |  |
| `evolution.tree.artifact_ledger:LEDGER_FILE` | `evolution.tree.artifacts.artifact_ledger:LEDGER_FILE` |  |
| `evolution.tree.artifact_ledger:LEDGER_LOCK` | `evolution.tree.artifacts.artifact_ledger:LEDGER_LOCK` |  |
| `evolution.tree.artifact_ledger:MAX_LEDGER_BYTES` | `evolution.tree.artifacts.artifact_ledger:MAX_LEDGER_BYTES` |  |
| `evolution.tree.artifact_ledger:MAX_LEDGER_RECORD_BYTES` | `evolution.tree.artifacts.artifact_ledger:MAX_LEDGER_RECORD_BYTES` |  |
| `evolution.tree.artifact_ledger:RESERVED_TERMINALS` | `evolution.tree.artifacts.artifact_schema:RESERVED_TERMINALS` |  |
| `evolution.tree.artifact_ledger:Snapshot` | `evolution.tree.storage.append_log:Snapshot` |  |
| `evolution.tree.artifact_ledger:__all__` | `evolution.tree.artifacts.artifact_ledger:__all__` |  |
| `evolution.tree.artifact_ledger:_fault` | `evolution.tree.storage.append_log:_fault` |  |
| `evolution.tree.artifact_ledger:_guard_layout` | `evolution.tree.artifacts.artifact_ledger:_guard_layout` |  |
| `evolution.tree.artifact_ledger:_package_version` | `evolution.tree.artifacts.artifact_ledger:_package_version` |  |
| `evolution.tree.artifact_ledger:_record_cap` | `evolution.tree.artifacts.artifact_ledger:_record_cap` |  |
| `evolution.tree.artifact_ledger:inspect_ledger` | `evolution.tree.artifacts.artifact_ledger:inspect_ledger` |  |
| `evolution.tree.artifact_ledger:inspect_log` | `evolution.tree.storage.append_log:inspect_log` |  |
| `evolution.tree.artifact_ledger:resolve_package_version` | `evolution.tree.execution.journal_identity:resolve_package_version` |  |
| `evolution.tree.artifact_payloads:ArtifactCorruptionError` | `evolution.tree.artifacts.artifact_errors:ArtifactCorruptionError` |  |
| `evolution.tree.artifact_payloads:ArtifactUnsafeError` | `evolution.tree.artifacts.artifact_errors:ArtifactUnsafeError` |  |
| `evolution.tree.artifact_payloads:AuxiliaryDeclaration` | `evolution.tree.artifacts.artifact_payloads:AuxiliaryDeclaration` |  |
| `evolution.tree.artifact_payloads:AuxiliaryFile` | `evolution.tree.artifacts.artifact_payloads:AuxiliaryFile` |  |
| `evolution.tree.artifact_payloads:AuxiliaryPublication` | `evolution.tree.artifacts.artifact_projections:AuxiliaryPublication` | |
| `evolution.tree.artifact_payloads:AuxiliarySourceReference` | `evolution.tree.artifacts.artifact_payloads:AuxiliarySourceReference` |  |
| `evolution.tree.artifact_payloads:JournalFault` | `evolution.tree.storage.journal_errors:JournalFault` |  |
| `evolution.tree.artifact_payloads:Snapshot` | `evolution.tree.storage.append_log:Snapshot` |  |
| `evolution.tree.artifact_payloads:_CHUNK` | `evolution.tree.artifacts.artifact_payloads:_CHUNK` |  |
| `evolution.tree.artifact_payloads:_FORMATS` | `evolution.tree.artifacts.artifact_payloads:_FORMATS` |  |
| `evolution.tree.artifact_payloads:_LOCATOR` | `evolution.tree.artifacts.artifact_payloads:_LOCATOR` |  |
| `evolution.tree.artifact_payloads:_SHA` | `evolution.tree.artifacts.artifact_payloads:_SHA` |  |
| `evolution.tree.artifact_payloads:_check_files` | `evolution.tree.artifacts.artifact_payloads:_check_files` |  |
| `evolution.tree.artifact_payloads:_directory` | `evolution.tree.storage.append_log:_directory` |  |
| `evolution.tree.artifact_payloads:_file_digest` | `evolution.tree.artifacts.artifact_payloads:_file_digest` |  |
| `evolution.tree.artifact_payloads:_identifier` | `evolution.tree.storage.envelope:_identifier` |  |
| `evolution.tree.artifact_payloads:_integer` | `evolution.tree.storage.envelope:_integer` |  |
| `evolution.tree.artifact_payloads:_keys` | `evolution.tree.storage.envelope:_keys` |  |
| `evolution.tree.artifact_payloads:_open_directory` | `evolution.tree.artifacts.artifact_payloads:_open_directory` |  |
| `evolution.tree.artifact_payloads:_require` | `evolution.tree.storage.envelope:_require` |  |
| `evolution.tree.artifact_payloads:_sha` | `evolution.tree.artifacts.artifact_payloads:_sha` |  |
| `evolution.tree.artifact_payloads:canonical_json` | `evolution.tree.execution.journal_identity:canonical_json` |  |
| `evolution.tree.artifact_payloads:describe_auxiliary` | `evolution.tree.artifacts.artifact_payloads:describe_auxiliary` |  |
| `evolution.tree.artifact_payloads:describe_auxiliary_files` | `evolution.tree.artifacts.artifact_payloads:describe_auxiliary_files` |  |
| `evolution.tree.artifact_payloads:destination_present` | `evolution.tree.artifacts.artifact_payloads:destination_present` |  |
| `evolution.tree.artifact_payloads:finish_directory` | `evolution.tree.artifacts.artifact_payloads:finish_directory` |  |
| `evolution.tree.artifact_payloads:inspect_auxiliary` | `evolution.tree.artifacts.artifact_projections:inspect_auxiliary` |  |
| `evolution.tree.artifact_payloads:project_auxiliary` | `evolution.tree.artifacts.artifact_projections:project_auxiliary` |  |
| `evolution.tree.artifact_payloads:publish_directory` | `evolution.tree.artifacts.artifact_payloads:publish_directory` |  |
| `evolution.tree.artifact_payloads:verify_directory` | `evolution.tree.artifacts.artifact_payloads:verify_directory` |  |
| `evolution.tree.artifact_policy:ARTIFACT_DOMAIN` | `evolution.tree.artifacts.artifact_policy:ARTIFACT_DOMAIN` |  |
| `evolution.tree.artifact_policy:ArtifactPolicyV1` | `evolution.tree.artifacts.artifact_policy:ArtifactPolicyV1` | |
| `evolution.tree.artifact_policy:ArtifactUnsafeError` | `evolution.tree.artifacts.artifact_errors:ArtifactUnsafeError` |  |
| `evolution.tree.artifact_policy:DEFAULT_EXCLUDES` | `evolution.tree.artifacts.artifact_policy:DEFAULT_EXCLUDES` |  |
| `evolution.tree.artifact_policy:DEPENDENCY_DOMAIN` | `evolution.tree.artifacts.artifact_policy:DEPENDENCY_DOMAIN` |  |
| `evolution.tree.artifact_policy:EMPTY_DEPENDENCY_DIGEST` | `evolution.tree.artifacts.artifact_policy:EMPTY_DEPENDENCY_DIGEST` |  |
| `evolution.tree.artifact_policy:Entry` | `evolution.tree.artifacts.artifact_policy:Entry` |  |
| `evolution.tree.artifact_policy:TreeScan` | `evolution.tree.artifacts.artifact_policy:TreeScan` | |
| `evolution.tree.artifact_policy:_digest` | `evolution.tree.artifacts.artifact_policy:_digest` |  |
| `evolution.tree.artifact_policy:_feed` | `evolution.tree.artifacts.artifact_policy:_feed` |  |
| `evolution.tree.artifact_policy:_segment_match` | `evolution.tree.artifacts.artifact_policy:_segment_match` |  |
| `evolution.tree.artifact_policy:changes` | `evolution.tree.artifacts.artifact_policy:changes` |  |
| `evolution.tree.artifact_policy:entry_set` | `evolution.tree.artifacts.artifact_policy:entry_set` | |
| `evolution.tree.artifact_policy:matches` | `evolution.tree.artifacts.artifact_policy:matches` |  |
| `evolution.tree.artifact_policy:reraise_walk_error` | `evolution.tree.artifacts.artifact_policy:reraise_walk_error` |  |
| `evolution.tree.artifact_policy:scan_tree` | `evolution.tree.artifacts.artifact_policy:scan_tree` | |
| `evolution.tree.artifact_policy:tree_inodes` | `evolution.tree.artifacts.artifact_policy:tree_inodes` |  |
| `evolution.tree.artifact_policy:validate_pattern` | `evolution.tree.artifacts.artifact_policy:validate_pattern` |  |
| `evolution.tree.artifact_projections:ArtifactLifecycle` | `evolution.tree.artifacts.artifact_schema:ArtifactLifecycle` |  |
| `evolution.tree.artifact_projections:ArtifactPolicyV1` | `evolution.tree.artifacts.artifact_policy:ArtifactPolicyV1` |  |
| `evolution.tree.artifact_projections:ArtifactReference` | `evolution.tree.artifacts.artifact_projections:ArtifactReference` |  |
| `evolution.tree.artifact_projections:ArtifactUnsafeError` | `evolution.tree.artifacts.artifact_errors:ArtifactUnsafeError` |  |
| `evolution.tree.artifact_projections:CANDIDATE_LOCATOR` | `evolution.tree.artifacts.artifact_projections:CANDIDATE_LOCATOR` |  |
| `evolution.tree.artifact_projections:CANDIDATE_STATES` | `evolution.tree.artifacts.artifact_projections:CANDIDATE_STATES` |  |
| `evolution.tree.artifact_projections:CHAMPION_LOCATOR` | `evolution.tree.artifacts.artifact_projections:CHAMPION_LOCATOR` |  |
| `evolution.tree.artifact_projections:CORRUPTION_CODES` | `evolution.tree.artifacts.artifact_projections:CORRUPTION_CODES` |  |
| `evolution.tree.artifact_projections:CandidateEntry` | `evolution.tree.artifacts.artifact_projections:CandidateEntry` |  |
| `evolution.tree.artifact_projections:CandidateState` | `evolution.tree.artifacts.artifact_projections:CandidateState` |  |
| `evolution.tree.artifact_projections:ChampionEntry` | `evolution.tree.artifacts.artifact_projections:ChampionEntry` |  |
| `evolution.tree.artifact_projections:LineageProjection` | `evolution.tree.artifacts.artifact_projections:LineageProjection` |  |
| `evolution.tree.artifact_projections:STORE_STATES` | `evolution.tree.artifacts.artifact_projections:STORE_STATES` |  |
| `evolution.tree.artifact_projections:Snapshot` | `evolution.tree.storage.append_log:Snapshot` |  |
| `evolution.tree.artifact_projections:StoreState` | `evolution.tree.artifacts.artifact_projections:StoreState` |  |
| `evolution.tree.artifact_projections:_SHA256` | `evolution.tree.artifacts.artifact_projections:_SHA256` |  |
| `evolution.tree.artifact_projections:_STATE_OF` | `evolution.tree.artifacts.artifact_projections:_STATE_OF` |  |
| `evolution.tree.artifact_projections:__all__` | `evolution.tree.artifacts.artifact_projections:__all__` |  |
| `evolution.tree.artifact_projections:_corruption` | `evolution.tree.artifacts.artifact_projections:_corruption` |  |
| `evolution.tree.artifact_projections:_identifier` | `evolution.tree.artifacts.artifact_projections:_identifier` |  |
| `evolution.tree.artifact_projections:_reference` | `evolution.tree.artifacts.artifact_projections:_reference` |  |
| `evolution.tree.artifact_projections:_require` | `evolution.tree.artifacts.artifact_projections:_require` |  |
| `evolution.tree.artifact_projections:_sha` | `evolution.tree.artifacts.artifact_projections:_sha` |  |
| `evolution.tree.artifact_projections:corruption_code` | `evolution.tree.artifacts.artifact_projections:corruption_code` |  |
| `evolution.tree.artifact_projections:entry_set` | `evolution.tree.artifacts.artifact_policy:entry_set` |  |
| `evolution.tree.artifact_projections:is_identifier` | `evolution.tree.storage.envelope:is_identifier` |  |
| `evolution.tree.artifact_projections:project_lineage` | `evolution.tree.artifacts.artifact_projections:project_lineage` |  |
| `evolution.tree.artifact_projections:scan_tree` | `evolution.tree.artifacts.artifact_policy:scan_tree` |  |
| `evolution.tree.artifact_recovery:CopyOutcome` | `evolution.tree.artifacts.artifact_recovery:CopyOutcome` |  |
| `evolution.tree.artifact_recovery:Envelope` | `evolution.tree.storage.envelope:Envelope` |  |
| `evolution.tree.artifact_recovery:RETRIABLE_FAILURES` | `evolution.tree.artifacts.artifact_recovery:RETRIABLE_FAILURES` |  |
| `evolution.tree.artifact_recovery:RecoveryReport` | `evolution.tree.artifacts.artifact_recovery:RecoveryReport` |  |
| `evolution.tree.artifact_recovery:__all__` | `evolution.tree.artifacts.artifact_recovery:__all__` |  |
| `evolution.tree.artifact_recovery:copy_outcomes` | `evolution.tree.artifacts.artifact_recovery:copy_outcomes` |  |
| `evolution.tree.artifact_schema:ABANDON_REASONS` | `evolution.tree.artifacts.artifact_schema:ABANDON_REASONS` |  |
| `evolution.tree.artifact_schema:ARTIFACT_FAMILY` | `evolution.tree.artifacts.artifact_schema:ARTIFACT_FAMILY` |  |
| `evolution.tree.artifact_schema:ArtifactLifecycle` | `evolution.tree.artifacts.artifact_schema:ArtifactLifecycle` |  |
| `evolution.tree.artifact_schema:ArtifactPolicyV1` | `evolution.tree.artifacts.artifact_policy:ArtifactPolicyV1` |  |
| `evolution.tree.artifact_schema:ArtifactStoreIdentityV1` | `evolution.tree.artifacts.artifact_schema:ArtifactStoreIdentityV1` | |
| `evolution.tree.artifact_schema:AuxiliaryDeclaration` | `evolution.tree.artifacts.artifact_payloads:AuxiliaryDeclaration` |  |
| `evolution.tree.artifact_schema:COPY_FAILURES` | `evolution.tree.artifacts.artifact_schema:COPY_FAILURES` |  |
| `evolution.tree.artifact_schema:Envelope` | `evolution.tree.storage.envelope:Envelope` |  |
| `evolution.tree.artifact_schema:EnvelopeFamily` | `evolution.tree.storage.envelope:EnvelopeFamily` |  |
| `evolution.tree.artifact_schema:KINDS` | `evolution.tree.artifacts.artifact_schema:KINDS` |  |
| `evolution.tree.artifact_schema:LifecycleTransition` | `evolution.tree.storage.envelope:LifecycleTransition` |  |
| `evolution.tree.artifact_schema:NO_OPERATION_EVENTS` | `evolution.tree.artifacts.artifact_schema:NO_OPERATION_EVENTS` |  |
| `evolution.tree.artifact_schema:OPERATIONS` | `evolution.tree.artifacts.artifact_schema:OPERATIONS` |  |
| `evolution.tree.artifact_schema:RESERVED_TERMINALS` | `evolution.tree.artifacts.artifact_schema:RESERVED_TERMINALS` |  |
| `evolution.tree.artifact_schema:SOURCES` | `evolution.tree.artifacts.artifact_schema:SOURCES` |  |
| `evolution.tree.artifact_schema:STARTS` | `evolution.tree.artifacts.artifact_schema:STARTS` |  |
| `evolution.tree.artifact_schema:STOP_CODES` | `evolution.tree.artifacts.artifact_schema:STOP_CODES` |  |
| `evolution.tree.artifact_schema:STORE_PROFILE` | `evolution.tree.artifacts.artifact_schema:STORE_PROFILE` |  |
| `evolution.tree.artifact_schema:TERMINALS` | `evolution.tree.artifacts.artifact_schema:TERMINALS` |  |
| `evolution.tree.artifact_schema:_SHA256` | `evolution.tree.artifacts.artifact_schema:_SHA256` |  |
| `evolution.tree.artifact_schema:_UNSEALED` | `evolution.tree.artifacts.artifact_schema:_UNSEALED` |  |
| `evolution.tree.artifact_schema:_identifier` | `evolution.tree.storage.envelope:_identifier` |  |
| `evolution.tree.artifact_schema:_integer` | `evolution.tree.storage.envelope:_integer` |  |
| `evolution.tree.artifact_schema:_keys` | `evolution.tree.storage.envelope:_keys` |  |
| `evolution.tree.artifact_schema:_locator` | `evolution.tree.artifacts.artifact_schema:_locator` |  |
| `evolution.tree.artifact_schema:_require` | `evolution.tree.storage.envelope:_require` |  |
| `evolution.tree.artifact_schema:_sha` | `evolution.tree.artifacts.artifact_schema:_sha` |  |
| `evolution.tree.artifact_schema:_validate_payload` | `evolution.tree.artifacts.artifact_schema:_validate_payload` |  |
| `evolution.tree.artifact_schema:canonical_json` | `evolution.tree.execution.journal_identity:canonical_json` |  |
| `evolution.tree.artifacts:ARTIFACTS_DIR` | `evolution.tree.artifacts.artifact_ledger:ARTIFACTS_DIR` |  |
| `evolution.tree.artifacts:ARTIFACT_FAMILY` | `evolution.tree.artifacts.artifact_schema:ARTIFACT_FAMILY` |  |
| `evolution.tree.artifacts:ArtifactAdmissionRefused` | `evolution.tree.artifacts.artifact_errors:ArtifactAdmissionRefused` |  |
| `evolution.tree.artifacts:ArtifactCapacityError` | `evolution.tree.artifacts.artifact_errors:ArtifactCapacityError` |  |
| `evolution.tree.artifacts:ArtifactCorruptionError` | `evolution.tree.artifacts.artifact_errors:ArtifactCorruptionError` |  |
| `evolution.tree.artifacts:ArtifactError` | `evolution.tree.execution.candidate_integrity:ArtifactError` |  |
| `evolution.tree.artifacts:ArtifactLedger` | `evolution.tree.artifacts.artifact_ledger:ArtifactLedger` |  |
| `evolution.tree.artifacts:ArtifactLifecycle` | `evolution.tree.artifacts.artifact_schema:ArtifactLifecycle` |  |
| `evolution.tree.artifacts:ArtifactPolicyV1` | `evolution.tree.artifacts.artifact_policy:ArtifactPolicyV1` |  |
| `evolution.tree.artifacts:ArtifactRef` | `evolution.tree.artifacts.artifacts:ArtifactRef` |  |
| `evolution.tree.artifacts:ArtifactStore` | `evolution.tree.artifacts.artifacts:ArtifactStore` | |
| `evolution.tree.artifacts:ArtifactStoreIdentityV1` | `evolution.tree.artifacts.artifact_schema:ArtifactStoreIdentityV1` |  |
| `evolution.tree.artifacts:ArtifactStoreStopped` | `evolution.tree.artifacts.artifact_errors:ArtifactStoreStopped` |  |
| `evolution.tree.artifacts:ArtifactUnsafeError` | `evolution.tree.artifacts.artifact_errors:ArtifactUnsafeError` |  |
| `evolution.tree.artifacts:AuxiliaryDeclaration` | `evolution.tree.artifacts.artifact_payloads:AuxiliaryDeclaration` |  |
| `evolution.tree.artifacts:AuxiliaryPublication` | `evolution.tree.artifacts.artifact_projections:AuxiliaryPublication` |  |
| `evolution.tree.artifacts:CANDIDATES` | `evolution.tree.artifacts.artifacts:CANDIDATES` |  |
| `evolution.tree.artifacts:CHAMPIONS` | `evolution.tree.artifacts.artifacts:CHAMPIONS` |  |
| `evolution.tree.artifacts:CandidateView` | `evolution.tree.execution.interaction:CandidateView` |  |
| `evolution.tree.artifacts:CapacityEstimate` | `evolution.tree.artifacts.artifacts:CapacityEstimate` |  |
| `evolution.tree.artifacts:CopyOutcome` | `evolution.tree.artifacts.artifact_recovery:CopyOutcome` |  |
| `evolution.tree.artifacts:Envelope` | `evolution.tree.storage.envelope:Envelope` |  |
| `evolution.tree.artifacts:ExpectedAttestation` | `evolution.tree.artifacts.artifacts:ExpectedAttestation` | |
| `evolution.tree.artifacts:InodeNeed` | `evolution.tree.storage.inode_floor:InodeNeed` |  |
| `evolution.tree.artifacts:JournalFault` | `evolution.tree.storage.journal_errors:JournalFault` |  |
| `evolution.tree.artifacts:Kind` | `evolution.tree.artifacts.artifacts:Kind` |  |
| `evolution.tree.artifacts:LineageProjection` | `evolution.tree.artifacts.artifact_projections:LineageProjection` |  |
| `evolution.tree.artifacts:MAX_LEDGER_BYTES` | `evolution.tree.artifacts.artifact_ledger:MAX_LEDGER_BYTES` |  |
| `evolution.tree.artifacts:RecoveryReport` | `evolution.tree.artifacts.artifact_recovery:RecoveryReport` |  |
| `evolution.tree.artifacts:STAGING` | `evolution.tree.artifacts.artifacts:STAGING` |  |
| `evolution.tree.artifacts:STARTS` | `evolution.tree.artifacts.artifact_schema:STARTS` |  |
| `evolution.tree.artifacts:SelectionDecision` | `evolution.tree.artifacts.artifacts:SelectionDecision` |  |
| `evolution.tree.artifacts:Source` | `evolution.tree.artifacts.artifacts:Source` |  |
| `evolution.tree.artifacts:TreeScan` | `evolution.tree.artifacts.artifact_policy:TreeScan` |  |
| `evolution.tree.artifacts:WORKSPACES` | `evolution.tree.artifacts.artifacts:WORKSPACES` |  |
| `evolution.tree.artifacts:_ABANDONABLE` | `evolution.tree.artifacts.artifacts:_ABANDONABLE` |  |
| `evolution.tree.artifacts:_DERIVED` | `evolution.tree.artifacts.artifacts:_DERIVED` |  |
| `evolution.tree.artifacts:_SUBDIRECTORIES` | `evolution.tree.artifacts.artifacts:_SUBDIRECTORIES` |  |
| `evolution.tree.artifacts:__all__` | `evolution.tree.artifacts.artifacts:__all__` |  |
| `evolution.tree.artifacts:_check_identifier` | `evolution.tree.artifacts.artifacts:_check_identifier` |  |
| `evolution.tree.artifacts:_estimate` | `evolution.tree.artifacts.artifacts:_estimate` |  |
| `evolution.tree.artifacts:_make_private_directory` | `evolution.tree.artifacts.artifacts:_make_private_directory` |  |
| `evolution.tree.artifacts:_observe_estimate` | `evolution.tree.artifacts.artifacts:_observe_estimate` |  |
| `evolution.tree.artifacts:_pending_payload` | `evolution.tree.artifacts.artifacts:_pending_payload` |  |
| `evolution.tree.artifacts:_scan_published` | `evolution.tree.artifacts.artifacts:_scan_published` |  |
| `evolution.tree.artifacts:_scan_seed` | `evolution.tree.artifacts.artifacts:_scan_seed` |  |
| `evolution.tree.artifacts:changes` | `evolution.tree.artifacts.artifact_policy:changes` |  |
| `evolution.tree.artifacts:check_destination_parent` | `evolution.tree.artifacts.artifact_copy:check_destination_parent` |  |
| `evolution.tree.artifacts:copy_outcomes` | `evolution.tree.artifacts.artifact_recovery:copy_outcomes` |  |
| `evolution.tree.artifacts:corruption_code` | `evolution.tree.artifacts.artifact_projections:corruption_code` |  |
| `evolution.tree.artifacts:destination_present` | `evolution.tree.artifacts.artifact_payloads:destination_present` |  |
| `evolution.tree.artifacts:entry_set` | `evolution.tree.artifacts.artifact_policy:entry_set` |  |
| `evolution.tree.artifacts:finish_directory` | `evolution.tree.artifacts.artifact_payloads:finish_directory` |  |
| `evolution.tree.artifacts:fsync_tree` | `evolution.tree.artifacts.artifact_copy:fsync_tree` |  |
| `evolution.tree.artifacts:is_identifier` | `evolution.tree.storage.envelope:is_identifier` |  |
| `evolution.tree.artifacts:map_os_error` | `evolution.tree.artifacts.artifact_copy:map_os_error` |  |
| `evolution.tree.artifacts:project_auxiliary` | `evolution.tree.artifacts.artifact_projections:project_auxiliary` |  |
| `evolution.tree.artifacts:project_lineage` | `evolution.tree.artifacts.artifact_projections:project_lineage` |  |
| `evolution.tree.artifacts:publish` | `evolution.tree.artifacts.artifact_copy:publish` |  |
| `evolution.tree.artifacts:publish_directory` | `evolution.tree.artifacts.artifact_payloads:publish_directory` |  |
| `evolution.tree.artifacts:remove_tree` | `evolution.tree.artifacts.artifact_copy:remove_tree` |  |
| `evolution.tree.artifacts:restore_writable` | `evolution.tree.artifacts.artifact_copy:restore_writable` |  |
| `evolution.tree.artifacts:scan_tree` | `evolution.tree.artifacts.artifact_policy:scan_tree` |  |
| `evolution.tree.artifacts:stage_copy` | `evolution.tree.artifacts.artifact_copy:stage_copy` |  |
| `evolution.tree.artifacts:tree_inodes` | `evolution.tree.artifacts.artifact_policy:tree_inodes` |  |
| `evolution.tree.artifacts:verify_directory` | `evolution.tree.artifacts.artifact_payloads:verify_directory` |  |
| `evolution.tree.attestation:ATTESTATION_CODES` | `evolution.tree.execution.interaction:ATTESTATION_CODES` |  |
| `evolution.tree.attestation:AdapterCapabilities` | `evolution.tree.execution.interaction:AdapterCapabilities` |  |
| `evolution.tree.attestation:ArtifactRef` | `evolution.tree.artifacts.artifacts:ArtifactRef` |  |
| `evolution.tree.attestation:AttestationCode` | `evolution.tree.execution.interaction:AttestationCode` |  |
| `evolution.tree.attestation:AttestationError` | `evolution.tree.execution.interaction:AttestationError` |  |
| `evolution.tree.attestation:ComponentsChanged` | `evolution.tree.artifacts.attestation:ComponentsChanged` |  |
| `evolution.tree.attestation:ExpectedAttestation` | `evolution.tree.artifacts.artifacts:ExpectedAttestation` |  |
| `evolution.tree.attestation:PromotionCapableAdapter` | `evolution.tree.artifacts.attestation:PromotionCapableAdapter` |  |
| `evolution.tree.attestation:REQUIRED_ATTESTS` | `evolution.tree.execution.wire_v2:REQUIRED_ATTESTS` |  |
| `evolution.tree.attestation:RolloutInfrastructureError` | `evolution.tree.execution.interaction:RolloutInfrastructureError` |  |
| `evolution.tree.attestation:SemanticIdentity` | `evolution.tree.execution.interaction:SemanticIdentity` |  |
| `evolution.tree.attestation:TargetAdapter` | `evolution.tree.execution.interaction:TargetAdapter` |  |
| `evolution.tree.attestation:TargetIdentity` | `evolution.tree.execution.interaction:TargetIdentity` |  |
| `evolution.tree.attestation:TargetPreflight` | `evolution.tree.artifacts.attestation:TargetPreflight` | |
| `evolution.tree.attestation:_REQUIRED` | `evolution.tree.artifacts.attestation:_REQUIRED` |  |
| `evolution.tree.attestation:verify_attestation` | `evolution.tree.artifacts.attestation:verify_attestation` |  |
| `evolution.tree.snapshot:Snapshot` | `evolution.tree.artifacts.snapshot:Snapshot` |  |
| `evolution.tree.snapshot:SnapshotError` | `evolution.tree.execution.candidate_integrity:SnapshotError` |  |
| `evolution.tree.snapshot:SnapshotStore` | `evolution.tree.artifacts.snapshot:SnapshotStore` |  |
| `evolution.tree.snapshot:_assert_regular_tree` | `evolution.tree.execution.candidate_integrity:_assert_regular_tree` |  |
| `evolution.tree.snapshot:_feed` | `evolution.tree.artifacts.snapshot:_feed` |  |
| `evolution.tree.snapshot:_set_frozen` | `evolution.tree.artifacts.snapshot:_set_frozen` |  |
| `evolution.tree.snapshot:_validate_snapshot_id` | `evolution.tree.artifacts.snapshot:_validate_snapshot_id` |  |
| `evolution.tree.snapshot:artifact_digest` | `evolution.tree.execution.candidate_integrity:artifact_digest` | |
| `evolution.tree.snapshot:has_hidden_path_segment` | `evolution.tree.execution.candidate_integrity:has_hidden_path_segment` |  |
| `evolution.tree.snapshot:snapshot_content_digest` | `evolution.tree.artifacts.snapshot:snapshot_content_digest` |  |
| `evolution.tree.snapshot:snapshot_digest` | `evolution.tree.artifacts.snapshot:snapshot_digest` |  |

### Target package import migration

| Old path → new defining path | Consumer |
|---|---|
| `evolution.tree.http_adapter:_CHUNK` → `evolution.tree.target.http_adapter:_CHUNK` | |
| `evolution.tree.http_adapter:_KNOWN_STRATEGIES` → `evolution.tree.target.http_adapter:_KNOWN_STRATEGIES` | |
| `evolution.tree.http_adapter:_REQUIRED_ATTESTS` → `evolution.tree.target.http_adapter:_REQUIRED_ATTESTS` | |
| `evolution.tree.http_adapter:_T` → `evolution.tree.target.http_adapter:_T` | |
| `evolution.tree.http_adapter:AdapterNotPreflighted` → `evolution.tree.target.http_adapter:AdapterNotPreflighted` | |
| `evolution.tree.http_adapter:_fail` → `evolution.tree.target.http_adapter:_fail` | |
| `evolution.tree.http_adapter:_known_strategies` → `evolution.tree.target.http_adapter:_known_strategies` | |
| `evolution.tree.http_adapter:is_json_media` → `evolution.tree.target.http_adapter:is_json_media` | |
| `evolution.tree.http_adapter:_plain` → `evolution.tree.target.http_adapter:_plain` | |
| `evolution.tree.http_adapter:Exchange` → `evolution.tree.target.http_adapter:Exchange` | |
| `evolution.tree.http_adapter:_DeadlineReader` → `evolution.tree.target.http_adapter:_DeadlineReader` | |
| `evolution.tree.http_adapter:_FramingReader` → `evolution.tree.target.http_adapter:_FramingReader` | |
| `evolution.tree.http_adapter:_DeadlineSocket` → `evolution.tree.target.http_adapter:_DeadlineSocket` | |
| `evolution.tree.http_adapter:_read_response` → `evolution.tree.target.http_adapter:_read_response` | |
| `evolution.tree.http_adapter:_answer_after_broken_write` → `evolution.tree.target.http_adapter:_answer_after_broken_write` | |
| `evolution.tree.http_adapter:HttpTransport` → `evolution.tree.target.http_adapter:HttpTransport` | |
| `evolution.tree.http_adapter:_require_json_media` → `evolution.tree.target.http_adapter:_require_json_media` | |
| `evolution.tree.http_adapter:_decode` → `evolution.tree.target.http_adapter:_decode` | |
| `evolution.tree.http_adapter:_wire_error_or_none` → `evolution.tree.target.http_adapter:_wire_error_or_none` | |
| `evolution.tree.http_adapter:_json_call` → `evolution.tree.target.http_adapter:_json_call` | |
| `evolution.tree.http_adapter:_released_id` → `evolution.tree.target.http_adapter:_released_id` | |
| `evolution.tree.http_adapter:_closed_id` → `evolution.tree.target.http_adapter:_closed_id` | |
| `evolution.tree.http_adapter:_delete` → `evolution.tree.target.http_adapter:_delete` | |
| `evolution.tree.http_adapter:_instances` → `evolution.tree.target.http_adapter:_instances` | |
| `evolution.tree.http_adapter:_epoch_guard` → `evolution.tree.target.http_adapter:_epoch_guard` | |
| `evolution.tree.http_adapter:_identity_of` → `evolution.tree.target.http_adapter:_identity_of` | |
| `evolution.tree.http_adapter:HttpSession` → `evolution.tree.target.http_adapter:HttpSession` | |
| `evolution.tree.http_adapter:HttpInstance` → `evolution.tree.target.http_adapter:HttpInstance` | |
| `evolution.tree.http_adapter:HttpSessionAdapter` → `evolution.tree.target.http_adapter:HttpSessionAdapter` | |
| `evolution.tree.target_server:RETRYABLE` → `evolution.tree.target.reference_target_http:RETRYABLE` | |
| `evolution.tree.target_server:_LIE_DIGEST` → `evolution.tree.target.target_server:_LIE_DIGEST` | |
| `evolution.tree.target_server:BackendError` → `evolution.tree.target.target_server:BackendError` | |
| `evolution.tree.target_server:BackendReply` → `evolution.tree.target.target_server:BackendReply` | |
| `evolution.tree.target_server:LoadedTree` → `evolution.tree.target.target_server:LoadedTree` | |
| `evolution.tree.target_server:BackendSession` → `evolution.tree.target.target_server:BackendSession` | |
| `evolution.tree.target_server:BackendInstance` → `evolution.tree.target.target_server:BackendInstance` | |
| `evolution.tree.target_server:DescribingInstance` → `evolution.tree.target.target_server:DescribingInstance` | |
| `evolution.tree.target_server:ComponentsInstance` → `evolution.tree.target.target_server:ComponentsInstance` | |
| `evolution.tree.target_server:TargetBackend` → `evolution.tree.target.target_server:TargetBackend` | |
| `evolution.tree.target_server:_Refused` → `evolution.tree.target.target_server:_Refused` | |
| `evolution.tree.target_server:_error_payload` → `evolution.tree.target.reference_target_http:_error_payload` | |
| `evolution.tree.target_server:_bad` → `evolution.tree.target.target_server:_bad` | |
| `evolution.tree.target_server:_reject_constant` → `evolution.tree.target.target_server:_reject_constant` | |
| `evolution.tree.target_server:_object` → `evolution.tree.target.target_server:_object` | |
| `evolution.tree.target_server:_fields` → `evolution.tree.target.target_server:_fields` | |
| `evolution.tree.target_server:_identifier` → `evolution.tree.target.target_server:_identifier` | |
| `evolution.tree.target_server:_seed` → `evolution.tree.target.target_server:_seed` | |
| `evolution.tree.target_server:_json_safe` → `evolution.tree.target.target_server:_json_safe` | |
| `evolution.tree.target_server:_inflate` → `evolution.tree.target.target_server:_inflate` | |
| `evolution.tree.target_server:Misbehave` → `evolution.tree.target.target_server:Misbehave` | |
| `evolution.tree.target_server:NEUTRAL_MISBEHAVE` → `evolution.tree.target.target_server:NEUTRAL_MISBEHAVE` | |
| `evolution.tree.target_server:_Record` → `evolution.tree.target.target_server:_Record` | |
| `evolution.tree.target_server:_Session` → `evolution.tree.target.target_server:_Session` | |
| `evolution.tree.target_server:_Instance` → `evolution.tree.target.target_server:_Instance` | |
| `evolution.tree.target_server:_discard_owned_root` → `evolution.tree.target.target_server:_discard_owned_root` | |
| `evolution.tree.target_server:_close_quietly` → `evolution.tree.target.target_server:_close_quietly` | |
| `evolution.tree.target_server:_require_instance_shape` → `evolution.tree.target.target_server:_require_instance_shape` | |
| `evolution.tree.target_server:_instance_description` → `evolution.tree.target.target_server:_instance_description` | |
| `evolution.tree.target_server:_instance_components` → `evolution.tree.target.target_server:_instance_components` | |
| `evolution.tree.target_server:_require_session_shape` → `evolution.tree.target.target_server:_require_session_shape` | |
| `evolution.tree.target_server:_replay` → `evolution.tree.target.target_server:_replay` | |
| `evolution.tree.target_server:TargetServer` → `evolution.tree.target.target_server:TargetServer` | |
| `evolution.tree.target_server:serve_target` → `evolution.tree.target.target_server:serve_target` | |
| `evolution.tree.target_calls:__all__` → `evolution.tree.target.target_calls:__all__` | |
| `evolution.tree.target_calls:Operation` → `evolution.tree.target.target_calls:Operation` | |
| `evolution.tree.target_calls:UncertainReason` → `evolution.tree.target.target_calls:UncertainReason` | |
| `evolution.tree.target_calls:NetworkOutcome` → `evolution.tree.target.target_calls:NetworkOutcome` | |
| `evolution.tree.target_calls:RecordState` → `evolution.tree.target.target_calls:RecordState` | |
| `evolution.tree.target_calls:_Class` → `evolution.tree.target.target_calls:_Class` | |
| `evolution.tree.target_calls:_MUTATING` → `evolution.tree.target.target_calls:_MUTATING` | |
| `evolution.tree.target_calls:_INSTANCES` → `evolution.tree.target.target_calls:_INSTANCES` | |
| `evolution.tree.target_calls:_SHAPES` → `evolution.tree.target.target_calls:_SHAPES` | |
| `evolution.tree.target_calls:TargetCallRecord` → `evolution.tree.target.target_calls:TargetCallRecord` | |
| `evolution.tree.target_calls:TargetCallSettlement` → `evolution.tree.target.target_calls:TargetCallSettlement` | |
| `evolution.tree.target_calls:_Call` → `evolution.tree.target.target_calls:_Call` | |
| `evolution.tree.target_calls:_parse` → `evolution.tree.target.target_calls:_parse` | |
| `evolution.tree.target_calls:network_outcome` → `evolution.tree.target.target_calls:network_outcome` | |
| `evolution.tree.target_calls:TargetCallLedger` → `evolution.tree.target.target_calls:TargetCallLedger` | |
| `evolution.tree.connection_contract:METRIC_CONTRACT_VERSION` → `evolution.tree.target.connection_contract:METRIC_CONTRACT_VERSION` | |
| `evolution.tree.connection_contract:ROLE_VISIBILITY_VERSION` → `evolution.tree.target.connection_contract:ROLE_VISIBILITY_VERSION` | |
| `evolution.tree.connection_contract:VERBATIM_ANSWERS_VERSION` → `evolution.tree.target.connection_contract:VERBATIM_ANSWERS_VERSION` | |
| `evolution.tree.connection_contract:SECRET_ORACLE_PATHS_VERSION` → `evolution.tree.target.connection_contract:SECRET_ORACLE_PATHS_VERSION` | |
| `evolution.tree.connection_contract:MAX_SECRET_ORACLE_PATHS` → `evolution.tree.target.connection_contract:MAX_SECRET_ORACLE_PATHS` | |
| `evolution.tree.connection_contract:_SECRET_PATHS_KEYS` → `evolution.tree.target.connection_contract:_SECRET_PATHS_KEYS` | |
| `evolution.tree.connection_contract:MAX_VERBATIM_ANSWERS` → `evolution.tree.target.connection_contract:MAX_VERBATIM_ANSWERS` | |
| `evolution.tree.connection_contract:MAX_VERBATIM_ANSWER_BYTES` → `evolution.tree.target.connection_contract:MAX_VERBATIM_ANSWER_BYTES` | |
| `evolution.tree.connection_contract:_VERBATIM_KEYS` → `evolution.tree.target.connection_contract:_VERBATIM_KEYS` | |
| `evolution.tree.connection_contract:ValueRule` → `evolution.tree.target.connection_contract:ValueRule` | |
| `evolution.tree.connection_contract:VALUE_RULES` → `evolution.tree.target.connection_contract:VALUE_RULES` | |
| `evolution.tree.connection_contract:MAX_TARGET_FIELDS` → `evolution.tree.target.connection_contract:MAX_TARGET_FIELDS` | |
| `evolution.tree.connection_contract:MAX_EVENT_BYTES` → `evolution.tree.target.connection_contract:MAX_EVENT_BYTES` | |
| `evolution.tree.connection_contract:MAX_POINTER_BYTES` → `evolution.tree.target.connection_contract:MAX_POINTER_BYTES` | |
| `evolution.tree.connection_contract:MAX_CONTRACT_BYTES` → `evolution.tree.target.connection_contract:MAX_CONTRACT_BYTES` | |
| `evolution.tree.connection_contract:_CONTRACT_KEYS` → `evolution.tree.target.connection_contract:_CONTRACT_KEYS` | |
| `evolution.tree.connection_contract:_FIELD_KEYS` → `evolution.tree.target.connection_contract:_FIELD_KEYS` | |
| `evolution.tree.connection_contract:_VISIBILITY_KEYS` → `evolution.tree.target.connection_contract:_VISIBILITY_KEYS` | |
| `evolution.tree.connection_contract:ContractError` → `evolution.tree.target.connection_contract:ContractError` | |
| `evolution.tree.connection_contract:_join` → `evolution.tree.target.connection_contract:_join` | |
| `evolution.tree.connection_contract:_closed` → `evolution.tree.target.connection_contract:_closed` | |
| `evolution.tree.connection_contract:_utf8_length` → `evolution.tree.target.connection_contract:_utf8_length` | |
| `evolution.tree.connection_contract:TargetField` → `evolution.tree.target.connection_contract:TargetField` | |
| `evolution.tree.connection_contract:MetricContract` → `evolution.tree.target.connection_contract:MetricContract` | |
| `evolution.tree.connection_contract:RoleVisibility` → `evolution.tree.target.connection_contract:RoleVisibility` | |
| `evolution.tree.connection_contract:VerbatimAnswers` → `evolution.tree.target.connection_contract:VerbatimAnswers` | |
| `evolution.tree.connection_contract:SecretOraclePaths` → `evolution.tree.target.connection_contract:SecretOraclePaths` | |
| `evolution.tree.connection_contract:ConnectionContract` → `evolution.tree.target.connection_contract:ConnectionContract` | |
| `evolution.tree.connection_contract:_version` → `evolution.tree.target.connection_contract:_version` | |
| `evolution.tree.connection_contract:_event` → `evolution.tree.target.connection_contract:_event` | |
| `evolution.tree.connection_contract:_pointer` → `evolution.tree.target.connection_contract:_pointer` | |
| `evolution.tree.connection_contract:_value_rule` → `evolution.tree.target.connection_contract:_value_rule` | |
| `evolution.tree.connection_contract:parse_metric_contract` → `evolution.tree.target.connection_contract:parse_metric_contract` | |
| `evolution.tree.connection_contract:parse_verbatim_answers` → `evolution.tree.target.connection_contract:parse_verbatim_answers` | |
| `evolution.tree.connection_contract:parse_role_visibility` → `evolution.tree.target.connection_contract:parse_role_visibility` | |
| `evolution.tree.connection_contract:parse_secret_oracle_paths` → `evolution.tree.target.connection_contract:parse_secret_oracle_paths` | |
| `evolution.tree.connection_probe:CONNECTION_BINDING_VERSION` → `evolution.tree.target.connection_probe:CONNECTION_BINDING_VERSION` | |
| `evolution.tree.connection_probe:PROBE_PROFILE` → `evolution.tree.target.connection_probe:PROBE_PROFILE` | |
| `evolution.tree.connection_probe:MAX_PATH_BYTES` → `evolution.tree.target.connection_probe:MAX_PATH_BYTES` | |
| `evolution.tree.connection_probe:DetailCode` → `evolution.tree.target.connection_probe:DetailCode` | |
| `evolution.tree.connection_probe:DETAIL_CODES` → `evolution.tree.target.connection_probe:DETAIL_CODES` | |
| `evolution.tree.connection_probe:OutcomeStatus` → `evolution.tree.target.connection_probe:OutcomeStatus` | |
| `evolution.tree.connection_probe:OUTCOME_STATUSES` → `evolution.tree.target.connection_probe:OUTCOME_STATUSES` | |
| `evolution.tree.connection_probe:Cleanup` → `evolution.tree.target.connection_probe:Cleanup` | |
| `evolution.tree.connection_probe:CLEANUP_STATES` → `evolution.tree.target.connection_probe:CLEANUP_STATES` | |
| `evolution.tree.connection_probe:ObservedType` → `evolution.tree.target.connection_probe:ObservedType` | |
| `evolution.tree.connection_probe:ExpectedType` → `evolution.tree.target.connection_probe:ExpectedType` | |
| `evolution.tree.connection_probe:PRIMARY_MANIFEST_PATH` → `evolution.tree.target.connection_probe:PRIMARY_MANIFEST_PATH` | |
| `evolution.tree.connection_probe:SCORER_MANIFEST_PATH` → `evolution.tree.target.connection_probe:SCORER_MANIFEST_PATH` | |
| `evolution.tree.connection_probe:SCORER_OUTPUT_PATH` → `evolution.tree.target.connection_probe:SCORER_OUTPUT_PATH` | |
| `evolution.tree.connection_probe:SCORER_METRICS_PATH` → `evolution.tree.target.connection_probe:SCORER_METRICS_PATH` | |
| `evolution.tree.connection_probe:DESCRIPTION_OBSERVED_PATH` → `evolution.tree.target.connection_probe:DESCRIPTION_OBSERVED_PATH` | |
| `evolution.tree.connection_probe:_SCORER_OPERATIONAL` → `evolution.tree.target.connection_probe:_SCORER_OPERATIONAL` | |
| `evolution.tree.connection_probe:_SCORER_METRIC` → `evolution.tree.target.connection_probe:_SCORER_METRIC` | |
| `evolution.tree.connection_probe:_SCORER_OUTPUT` → `evolution.tree.target.connection_probe:_SCORER_OUTPUT` | |
| `evolution.tree.connection_probe:SCORER_FAILED_CAUSE` → `evolution.tree.target.connection_probe:SCORER_FAILED_CAUSE` | |
| `evolution.tree.connection_probe:CLEANUP_FAILED_CAUSE` → `evolution.tree.target.connection_probe:CLEANUP_FAILED_CAUSE` | |
| `evolution.tree.connection_probe:INFRASTRUCTURE_CAUSE` → `evolution.tree.target.connection_probe:INFRASTRUCTURE_CAUSE` | |
| `evolution.tree.connection_probe:_UNAVAILABLE_TERMINALS` → `evolution.tree.target.connection_probe:_UNAVAILABLE_TERMINALS` | |
| `evolution.tree.connection_probe:_INCONCLUSIVE_TERMINALS` → `evolution.tree.target.connection_probe:_INCONCLUSIVE_TERMINALS` | |
| `evolution.tree.connection_probe:INFRASTRUCTURE_CAUSES` → `evolution.tree.target.connection_probe:INFRASTRUCTURE_CAUSES` | |
| `evolution.tree.connection_probe:CAUSE_CODES` → `evolution.tree.target.connection_probe:CAUSE_CODES` | |
| `evolution.tree.connection_probe:_PROBE_TERMINALS` → `evolution.tree.target.connection_probe:_PROBE_TERMINALS` | |
| `evolution.tree.connection_probe:_EXPECTED` → `evolution.tree.target.connection_probe:_EXPECTED` | |
| `evolution.tree.connection_probe:_CaseT` → `evolution.tree.target.connection_probe:_CaseT` | |
| `evolution.tree.connection_probe:cap_path` → `evolution.tree.target.connection_probe:cap_path` | |
| `evolution.tree.connection_probe:_utf8_bytes` → `evolution.tree.target.connection_probe:_utf8_bytes` | |
| `evolution.tree.connection_probe:target_field_manifest_path` → `evolution.tree.target.connection_probe:target_field_manifest_path` | |
| `evolution.tree.connection_probe:target_field_observed_path` → `evolution.tree.target.connection_probe:target_field_observed_path` | |
| `evolution.tree.connection_probe:primary_observed_path` → `evolution.tree.target.connection_probe:primary_observed_path` | |
| `evolution.tree.connection_probe:Diagnostic` → `evolution.tree.target.connection_probe:Diagnostic` | |
| `evolution.tree.connection_probe:_check_cause` → `evolution.tree.target.connection_probe:_check_cause` | |
| `evolution.tree.connection_probe:secret_path` → `evolution.tree.target.connection_probe:secret_path` | |
| `evolution.tree.connection_probe:journalable_path` → `evolution.tree.target.connection_probe:journalable_path` | |
| `evolution.tree.connection_probe:_ROLE_RANK` → `evolution.tree.target.connection_probe:_ROLE_RANK` | |
| `evolution.tree.connection_probe:secret_order` → `evolution.tree.target.connection_probe:secret_order` | |
| `evolution.tree.connection_probe:description_match` → `evolution.tree.target.connection_probe:description_match` | |
| `evolution.tree.connection_probe:utf8_bounded` → `evolution.tree.target.connection_probe:utf8_bounded` | |
| `evolution.tree.connection_probe:bounded_location` → `evolution.tree.target.connection_probe:bounded_location` | |
| `evolution.tree.connection_probe:description_secret` → `evolution.tree.target.connection_probe:description_secret` | |
| `evolution.tree.connection_probe:FieldFailure` → `evolution.tree.target.connection_probe:FieldFailure` | |
| `evolution.tree.connection_probe:ScorerFailure` → `evolution.tree.target.connection_probe:ScorerFailure` | |
| `evolution.tree.connection_probe:ProbeOutcome` → `evolution.tree.target.connection_probe:ProbeOutcome` | |
| `evolution.tree.connection_probe:_nonblank` → `evolution.tree.target.connection_probe:_nonblank` | |
| `evolution.tree.connection_probe:_index` → `evolution.tree.target.connection_probe:_index` | |
| `evolution.tree.connection_probe:binding_payload` → `evolution.tree.target.connection_probe:binding_payload` | |
| `evolution.tree.connection_probe:binding_digest` → `evolution.tree.target.connection_probe:binding_digest` | |
| `evolution.tree.connection_probe:section_digest` → `evolution.tree.target.connection_probe:section_digest` | |
| `evolution.tree.connection_probe:epoch_binding` → `evolution.tree.target.connection_probe:epoch_binding` | |
| `evolution.tree.connection_probe:probe_seed` → `evolution.tree.target.connection_probe:probe_seed` | |
| `evolution.tree.connection_probe:representative_case` → `evolution.tree.target.connection_probe:representative_case` | |
| `evolution.tree.connection_probe:history_events` → `evolution.tree.target.connection_probe:history_events` | |
| `evolution.tree.connection_probe:_json_kind` → `evolution.tree.target.connection_probe:_json_kind` | |
| `evolution.tree.connection_probe:observed_type` → `evolution.tree.target.connection_probe:observed_type` | |
| `evolution.tree.connection_probe:expected_type` → `evolution.tree.target.connection_probe:expected_type` | |
| `evolution.tree.connection_probe:_finite_json` → `evolution.tree.target.connection_probe:_finite_json` | |
| `evolution.tree.connection_probe:_value_failure` → `evolution.tree.target.connection_probe:_value_failure` | |
| `evolution.tree.connection_probe:check_event_fields` → `evolution.tree.target.connection_probe:check_event_fields` | |
| `evolution.tree.connection_probe:check_target_fields` → `evolution.tree.target.connection_probe:check_target_fields` | |
| `evolution.tree.connection_probe:_primary` → `evolution.tree.target.connection_probe:_primary` | |
| `evolution.tree.connection_probe:check_primary` → `evolution.tree.target.connection_probe:check_primary` | |
| `evolution.tree.connection_probe:_scorer_outcome` → `evolution.tree.target.connection_probe:_scorer_outcome` | |
| `evolution.tree.connection_probe:_unavailable` → `evolution.tree.target.connection_probe:_unavailable` | |
| `evolution.tree.connection_probe:classify` → `evolution.tree.target.connection_probe:classify` | |
| `evolution.tree.connection_probe:CONNECTION_DIR` → `evolution.tree.target.connection_probe:CONNECTION_DIR` | |
| `evolution.tree.connection_probe:CONNECTION_LOGS_DIR` → `evolution.tree.target.connection_probe:CONNECTION_LOGS_DIR` | |
| `evolution.tree.connection_probe:TRACE_FILE` → `evolution.tree.target.connection_probe:TRACE_FILE` | |
| `evolution.tree.connection_probe:TRACE_VERSION` → `evolution.tree.target.connection_probe:TRACE_VERSION` | |
| `evolution.tree.connection_probe:PROBE_SAMPLE_ID` → `evolution.tree.target.connection_probe:PROBE_SAMPLE_ID` | |
| `evolution.tree.connection_probe:MAX_TRACE_TEXT_BYTES` → `evolution.tree.target.connection_probe:MAX_TRACE_TEXT_BYTES` | |
| `evolution.tree.connection_probe:MAX_TRACE_LIST` → `evolution.tree.target.connection_probe:MAX_TRACE_LIST` | |
| `evolution.tree.connection_probe:_MAX_SEED` → `evolution.tree.target.connection_probe:_MAX_SEED` | |
| `evolution.tree.connection_probe:_NOT_PROBE_FIELDS` → `evolution.tree.target.connection_probe:_NOT_PROBE_FIELDS` | |
| `evolution.tree.connection_probe:_INTERRUPTIONS` → `evolution.tree.target.connection_probe:_INTERRUPTIONS` | |
| `evolution.tree.connection_probe:_TARGET_FAILURES` → `evolution.tree.target.connection_probe:_TARGET_FAILURES` | |
| `evolution.tree.connection_probe:_STOPPED_TERMINALS` → `evolution.tree.target.connection_probe:_STOPPED_TERMINALS` | |
| `evolution.tree.connection_probe:_ScorerT` → `evolution.tree.target.connection_probe:_ScorerT` | |
| `evolution.tree.connection_probe:probe_path` → `evolution.tree.target.connection_probe:probe_path` | |
| `evolution.tree.connection_probe:trace_path` → `evolution.tree.target.connection_probe:trace_path` | |
| `evolution.tree.connection_probe:probe_identity` → `evolution.tree.target.connection_probe:probe_identity` | |
| `evolution.tree.connection_probe:scorer_with_trace_root` → `evolution.tree.target.connection_probe:scorer_with_trace_root` | |
| `evolution.tree.connection_probe:ProbeResult` → `evolution.tree.target.connection_probe:ProbeResult` | |
| `evolution.tree.connection_probe:_Stages` → `evolution.tree.target.connection_probe:_Stages` | |
| `evolution.tree.connection_probe:run_probe` → `evolution.tree.target.connection_probe:run_probe` | |
| `evolution.tree.connection_probe:_probe` → `evolution.tree.target.connection_probe:_probe` | |
| `evolution.tree.connection_probe:_release` → `evolution.tree.target.connection_probe:_release` | |
| `evolution.tree.connection_probe:_cleanup_of` → `evolution.tree.target.connection_probe:_cleanup_of` | |
| `evolution.tree.connection_probe:_release_failures` → `evolution.tree.target.connection_probe:_release_failures` | |
| `evolution.tree.connection_probe:_reference` → `evolution.tree.target.connection_probe:_reference` | |
| `evolution.tree.connection_probe:_settled` → `evolution.tree.target.connection_probe:_settled` | |
| `evolution.tree.connection_probe:_bounded_code` → `evolution.tree.target.connection_probe:_bounded_code` | |
| `evolution.tree.connection_probe:_bounded_text` → `evolution.tree.target.connection_probe:_bounded_text` | |
| `evolution.tree.connection_probe:_outcome_fields` → `evolution.tree.target.connection_probe:_outcome_fields` | |
| `evolution.tree.connection_probe:_trace_document` → `evolution.tree.target.connection_probe:_trace_document` | |
| `evolution.tree.connection_probe:_write_trace` → `evolution.tree.target.connection_probe:_write_trace` | |
| `evolution.tree.connection_probe:_storage_fault` → `evolution.tree.target.connection_probe:_storage_fault` | |
| `evolution.tree.connection_probe:_snapshot` → `evolution.tree.target.connection_probe:_snapshot` | |
| `evolution.tree.connection_probe:_born` → `evolution.tree.target.connection_probe:_born` | |
| `evolution.tree.connection_probe:_trace_digest` → `evolution.tree.target.connection_probe:_trace_digest` | |
| `evolution.tree.connection_probe:probe_settlement` → `evolution.tree.target.connection_probe:probe_settlement` | |
| `evolution.tree.connection_probe:release_probe_leftover` → `evolution.tree.target.connection_probe:release_probe_leftover` | |
| `evolution.tree.probe_client:__all__` → `evolution.tree.target.probe_client:__all__` | |
| `evolution.tree.probe_client:_CAPABILITY` → `evolution.tree.target.probe_client:_CAPABILITY` | |
| `evolution.tree.probe_client:ProbeClientError` → `evolution.tree.target.probe_client:ProbeClientError` | |
| `evolution.tree.probe_client:_NoRedirect` → `evolution.tree.target.probe_client:_NoRedirect` | |
| `evolution.tree.probe_client:_validate_url` → `evolution.tree.target.probe_client:_validate_url` | |
| `evolution.tree.probe_client:_encode_request` → `evolution.tree.target.probe_client:_encode_request` | |
| `evolution.tree.probe_client:_read_response` → `evolution.tree.target.probe_client:_read_response` | |
| `evolution.tree.probe_client:call` → `evolution.tree.target.probe_client:call` | |
| `evolution.tree.probe_client:main` → `evolution.tree.target.probe_client:main` | |
| `evolution.tree.probe_mediator:__all__` → `evolution.tree.target.probe_mediator:__all__` | |
| `evolution.tree.probe_mediator:MAX_ACTIVE_HANDLERS` → `evolution.tree.target.probe_mediator:MAX_ACTIVE_HANDLERS` | |
| `evolution.tree.probe_mediator:MAX_HEADER_BYTES` → `evolution.tree.target.probe_mediator:MAX_HEADER_BYTES` | |
| `evolution.tree.probe_mediator:_LISTEN_BACKLOG` → `evolution.tree.target.probe_mediator:_LISTEN_BACKLOG` | |
| `evolution.tree.probe_mediator:_READ_TIMEOUT_SECONDS` → `evolution.tree.target.probe_mediator:_READ_TIMEOUT_SECONDS` | |
| `evolution.tree.probe_mediator:_WRITE_TIMEOUT_SECONDS` → `evolution.tree.target.probe_mediator:_WRITE_TIMEOUT_SECONDS` | |
| `evolution.tree.probe_mediator:_DEFAULT_CLOSE_GRACE_SECONDS` → `evolution.tree.target.probe_mediator:_DEFAULT_CLOSE_GRACE_SECONDS` | |
| `evolution.tree.probe_mediator:_CAPABILITY` → `evolution.tree.target.probe_mediator:_CAPABILITY` | |
| `evolution.tree.probe_mediator:_REJECTED` → `evolution.tree.target.probe_mediator:_REJECTED` | |
| `evolution.tree.probe_mediator:_FramingRejected` → `evolution.tree.target.probe_mediator:_FramingRejected` | |
| `evolution.tree.probe_mediator:_capability` → `evolution.tree.target.probe_mediator:_capability` | |
| `evolution.tree.probe_mediator:_json_object` → `evolution.tree.target.probe_mediator:_json_object` | |
| `evolution.tree.probe_mediator:_header_values` → `evolution.tree.target.probe_mediator:_header_values` | |
| `evolution.tree.probe_mediator:ProbeMediator` → `evolution.tree.target.probe_mediator:ProbeMediator` | |
| `evolution.tree.probe_session:__all__` → `evolution.tree.target.probe_session:__all__` | |
| `evolution.tree.probe_session:_REQUEST_ENVELOPE_BYTES` → `evolution.tree.target.probe_session:_REQUEST_ENVELOPE_BYTES` | |
| `evolution.tree.probe_session:_REQUEST_BODY_BYTES` → `evolution.tree.target.probe_session:_REQUEST_BODY_BYTES` | |
| `evolution.tree.probe_session:_RESPONSE_BYTES` → `evolution.tree.target.probe_session:_RESPONSE_BYTES` | |
| `evolution.tree.probe_session:_PRIVATE_RESPONSE_BYTES` → `evolution.tree.target.probe_session:_PRIVATE_RESPONSE_BYTES` | |
| `evolution.tree.probe_session:_ROLE_RESPONSE_CHARS` → `evolution.tree.target.probe_session:_ROLE_RESPONSE_CHARS` | |
| `evolution.tree.probe_session:_HEADER_BYTES` → `evolution.tree.target.probe_session:_HEADER_BYTES` | |
| `evolution.tree.probe_session:_CHUNK_LINE_BYTES` → `evolution.tree.target.probe_session:_CHUNK_LINE_BYTES` | |
| `evolution.tree.probe_session:_JSON_DEPTH` → `evolution.tree.target.probe_session:_JSON_DEPTH` | |
| `evolution.tree.probe_session:_DEFAULT_CLOSE_GRACE_SECONDS` → `evolution.tree.target.probe_session:_DEFAULT_CLOSE_GRACE_SECONDS` | |
| `evolution.tree.probe_session:_SECRET_SCRUB_PASSES` → `evolution.tree.target.probe_session:_SECRET_SCRUB_PASSES` | |
| `evolution.tree.probe_session:ProbeResult` → `evolution.tree.target.probe_session:ProbeResult` | |
| `evolution.tree.probe_session:AppendCallback` → `evolution.tree.target.probe_session:AppendCallback` | |
| `evolution.tree.probe_session:DiagnosticWriter` → `evolution.tree.target.probe_session:DiagnosticWriter` | |
| `evolution.tree.probe_session:_DETAILS` → `evolution.tree.target.probe_session:_DETAILS` | |
| `evolution.tree.probe_session:UNAVAILABLE_RESULT` → `evolution.tree.target.probe_session:UNAVAILABLE_RESULT` | |
| `evolution.tree.probe_session:_FAULT` → `evolution.tree.target.probe_session:_FAULT` | |
| `evolution.tree.probe_session:ProbeSessionAborted` → `evolution.tree.target.probe_session:ProbeSessionAborted` | |
| `evolution.tree.probe_session:_TransportFailure` → `evolution.tree.target.probe_session:_TransportFailure` | |
| `evolution.tree.probe_session:_Unreachable` → `evolution.tree.target.probe_session:_Unreachable` | |
| `evolution.tree.probe_session:_Request` → `evolution.tree.target.probe_session:_Request` | |
| `evolution.tree.probe_session:_Response` → `evolution.tree.target.probe_session:_Response` | |
| `evolution.tree.probe_session:_error` → `evolution.tree.target.probe_session:_error` | |
| `evolution.tree.probe_session:_validated_config` → `evolution.tree.target.probe_session:_validated_config` | |
| `evolution.tree.probe_session:_json_depth_and_shape` → `evolution.tree.target.probe_session:_json_depth_and_shape` | |
| `evolution.tree.probe_session:_json_bytes` → `evolution.tree.target.probe_session:_json_bytes` | |
| `evolution.tree.probe_session:_invalid_json_constant` → `evolution.tree.target.probe_session:_invalid_json_constant` | |
| `evolution.tree.probe_session:_request` → `evolution.tree.target.probe_session:_request` | |
| `evolution.tree.probe_session:_clip_utf8` → `evolution.tree.target.probe_session:_clip_utf8` | |
| `evolution.tree.probe_session:_safe_text` → `evolution.tree.target.probe_session:_safe_text` | |
| `evolution.tree.probe_session:_secret_prefix_overlap` → `evolution.tree.target.probe_session:_secret_prefix_overlap` | |
| `evolution.tree.probe_session:_secret_suffix_overlap` → `evolution.tree.target.probe_session:_secret_suffix_overlap` | |
| `evolution.tree.probe_session:_scrub_redacted_secret_suffix` → `evolution.tree.target.probe_session:_scrub_redacted_secret_suffix` | |
| `evolution.tree.probe_session:_bounded_response_text` → `evolution.tree.target.probe_session:_bounded_response_text` | |
| `evolution.tree.probe_session:write_probe_diagnostic` → `evolution.tree.target.probe_session:write_probe_diagnostic` | |
| `evolution.tree.probe_session:_encoded_target` → `evolution.tree.target.probe_session:_encoded_target` | |
| `evolution.tree.probe_session:_read_headers` → `evolution.tree.target.probe_session:_read_headers` | |
| `evolution.tree.probe_session:_read_chunked` → `evolution.tree.target.probe_session:_read_chunked` | |
| `evolution.tree.probe_session:_read_body` → `evolution.tree.target.probe_session:_read_body` | |
| `evolution.tree.probe_session:_http_exchange` → `evolution.tree.target.probe_session:_http_exchange` | |
| `evolution.tree.probe_session:ProbeSession` → `evolution.tree.target.probe_session:ProbeSession` | |
| `evolution.tree.probe_session:check_probe_health` → `evolution.tree.target.probe_session:check_probe_health` | |
| `evolution.tree.probe_wire:MAX_REQUEST_BYTES` → `evolution.tree.target.probe_wire:MAX_REQUEST_BYTES` | |
| `evolution.tree.probe_wire:MAX_RESPONSE_BYTES` → `evolution.tree.target.probe_wire:MAX_RESPONSE_BYTES` | |
| `evolution.tree.reference_target:NO_ATTESTED_SKILL` → `evolution.tree.target.reference_target:NO_ATTESTED_SKILL` | |
| `evolution.tree.reference_target:_skill_line` → `evolution.tree.target.reference_target:_skill_line` | |
| `evolution.tree.reference_target:_ReferenceSession` → `evolution.tree.target.reference_target:_ReferenceSession` | |
| `evolution.tree.reference_target:_ReferenceInstance` → `evolution.tree.target.reference_target:_ReferenceInstance` | |
| `evolution.tree.reference_target:ReferenceBackend` → `evolution.tree.target.reference_target:ReferenceBackend` | |
| `evolution.tree.reference_target:ReferenceTarget` → `evolution.tree.target.reference_target:ReferenceTarget` | |
| `evolution.tree.reference_target:serve` → `evolution.tree.target.reference_target:serve` | |
| `evolution.tree.reference_target_http:_CHUNKS` → `evolution.tree.target.reference_target_http:_CHUNKS` | |
| `evolution.tree.reference_target_http:_DRAIN_BYTES` → `evolution.tree.target.reference_target_http:_DRAIN_BYTES` | |
| `evolution.tree.reference_target_http:_DRAIN_SECONDS` → `evolution.tree.target.reference_target_http:_DRAIN_SECONDS` | |
| `evolution.tree.reference_target_http:_DRAIN_CHUNK` → `evolution.tree.target.reference_target_http:_DRAIN_CHUNK` | |
| `evolution.tree.reference_target_http:_TargetServer` → `evolution.tree.target.reference_target_http:_TargetServer` | |
| `evolution.tree.reference_target_http:_Handler` → `evolution.tree.target.reference_target_http:_Handler` | |
| `evolution.tree.reference_target_http:serve` → `evolution.tree.target.reference_target_http:serve` | |

### Roles package import migration

| Old path → new defining path | Consumer |
|---|---|
| `evolution.tree.agent_role:AgentPromptChanged` → `evolution.tree.roles.agent_role:AgentPromptChanged` | |
| `evolution.tree.agent_role:verify_agent_seals` → `evolution.tree.roles.agent_role:verify_agent_seals` | |
| `evolution.tree.agent_role:agent_log_secrets` → `evolution.tree.roles.agent_role:agent_log_secrets` | |
| `evolution.tree.agent_role:build_agent_backend` → `evolution.tree.roles.agent_role:build_agent_backend` | |
| `evolution.tree.agent_role:call_agent` → `evolution.tree.roles.agent_role:call_agent` | |
| `evolution.tree.role_call:VERSION` → `evolution.tree.roles.role_call:VERSION` | |
| `evolution.tree.role_call:ProviderErrorClass` → `evolution.tree.roles.role_call:ProviderErrorClass` | |
| `evolution.tree.role_call:T` → `evolution.tree.roles.role_call:T` | |
| `evolution.tree.role_call:_KEYS` → `evolution.tree.roles.role_call:_KEYS` | |
| `evolution.tree.role_call:_BACKOFF_KEYS` → `evolution.tree.roles.role_call:_BACKOFF_KEYS` | |
| `evolution.tree.role_call:_number` → `evolution.tree.roles.role_call:_number` | |
| `evolution.tree.role_call:_find_ssl_error_in_chain` → `evolution.tree.roles.role_call:_find_ssl_error_in_chain` | |
| `evolution.tree.role_call:RoleCallRetry` → `evolution.tree.roles.role_call:RoleCallRetry` | |
| `evolution.tree.role_call:classify_provider_error` → `evolution.tree.roles.role_call:classify_provider_error` | |
| `evolution.tree.role_call:_provider_exceptions` → `evolution.tree.roles.role_call:_provider_exceptions` | |
| `evolution.tree.role_call:_PROVIDER_CODE` → `evolution.tree.roles.role_call:_PROVIDER_CODE` | |
| `evolution.tree.role_call:provider_error_detail` → `evolution.tree.roles.role_call:provider_error_detail` | |
| `evolution.tree.role_call:AttemptTelemetry` → `evolution.tree.roles.role_call:AttemptTelemetry` | |
| `evolution.tree.role_call:RoleCallExhausted` → `evolution.tree.roles.role_call:RoleCallExhausted` | |
| `evolution.tree.role_call:ATTEMPT_LINE_STATUSES` → `evolution.tree.roles.role_call:ATTEMPT_LINE_STATUSES` | |
| `evolution.tree.role_call:_CREDENTIAL_STATUSES` → `evolution.tree.roles.role_call:_CREDENTIAL_STATUSES` | |
| `evolution.tree.role_call:_ATTEMPT_LINE` → `evolution.tree.roles.role_call:_ATTEMPT_LINE` | |
| `evolution.tree.role_call:AttemptSummary` → `evolution.tree.roles.role_call:AttemptSummary` | |
| `evolution.tree.role_call:_attempt_status` → `evolution.tree.roles.role_call:_attempt_status` | |
| `evolution.tree.role_call:attempt_line` → `evolution.tree.roles.role_call:attempt_line` | |
| `evolution.tree.role_call:is_attempt_line` → `evolution.tree.roles.role_call:is_attempt_line` | |
| `evolution.tree.role_call:AttemptReport` → `evolution.tree.roles.role_call:AttemptReport` | |
| `evolution.tree.role_call:AttemptRecorder` → `evolution.tree.roles.role_call:AttemptRecorder` | |
| `evolution.tree.role_call:call_with_retry` → `evolution.tree.roles.role_call:call_with_retry` | |
| `evolution.tree.role_failures:EDITOR_PROTOCOL_CATEGORIES` → `evolution.tree.roles.role_failures:EDITOR_PROTOCOL_CATEGORIES` | |
| `evolution.tree.role_failures:ADAPTIVE_GROUP_KEY_ORDER` → `evolution.tree.roles.role_failures:ADAPTIVE_GROUP_KEY_ORDER` | |
| `evolution.tree.role_failures:REFLECTION_DETAIL_CATEGORIES` → `evolution.tree.roles.role_failures:REFLECTION_DETAIL_CATEGORIES` | |
| `evolution.tree.role_failures:REFLECTION_DETAIL_PATH` → `evolution.tree.roles.role_failures:REFLECTION_DETAIL_PATH` | |
| `evolution.tree.role_failures:FailureClass` → `evolution.tree.roles.role_failures:FailureClass` | |
| `evolution.tree.role_failures:FAILURE_CLASSES` → `evolution.tree.roles.role_failures:FAILURE_CLASSES` | |
| `evolution.tree.role_failures:LIFECYCLE_ERRORS` → `evolution.tree.roles.role_failures:LIFECYCLE_ERRORS` | |
| `evolution.tree.role_failures:REFLECTION_REASONS` → `evolution.tree.roles.role_failures:REFLECTION_REASONS` | |
| `evolution.tree.role_failures:EDITOR_REASONS` → `evolution.tree.roles.role_failures:EDITOR_REASONS` | |
| `evolution.tree.role_failures:_REPORTED_REFLECTOR` → `evolution.tree.roles.role_failures:_REPORTED_REFLECTOR` | |
| `evolution.tree.role_failures:_REPORTED_EDITOR` → `evolution.tree.roles.role_failures:_REPORTED_EDITOR` | |
| `evolution.tree.role_failures:_error_key` → `evolution.tree.roles.role_failures:_error_key` | |
| `evolution.tree.role_failures:reflection_reason` → `evolution.tree.roles.role_failures:reflection_reason` | |
| `evolution.tree.role_failures:editor_reason` → `evolution.tree.roles.role_failures:editor_reason` | |
| `evolution.tree.role_failures:EDITOR_BUDGET_REASONS` → `evolution.tree.roles.role_failures:EDITOR_BUDGET_REASONS` | |
| `evolution.tree.role_failures:EDITOR_BUDGET_REASON_FAMILY` → `evolution.tree.roles.role_failures:EDITOR_BUDGET_REASON_FAMILY` | |
| `evolution.tree.role_failures:journaled_editor_reason` → `evolution.tree.roles.role_failures:journaled_editor_reason` | |
| `evolution.tree.role_failures:reflection_class` → `evolution.tree.roles.role_failures:reflection_class` | |
| `evolution.tree.role_failures:editor_class` → `evolution.tree.roles.role_failures:editor_class` | |
| `evolution.tree.role_failures:GROUPER_REASONS` → `evolution.tree.roles.role_failures:GROUPER_REASONS` | |
| `evolution.tree.role_failures:_GROUPER_SUSPENSION_FOR` → `evolution.tree.roles.role_failures:_GROUPER_SUSPENSION_FOR` | |
| `evolution.tree.role_failures:grouper_class` → `evolution.tree.roles.role_failures:grouper_class` | |
| `evolution.tree.role_failures:grouper_suspension_for` → `evolution.tree.roles.role_failures:grouper_suspension_for` | |
| `evolution.tree.role_failures:GrouperRoleFailure` → `evolution.tree.roles.role_failures:GrouperRoleFailure` | |
| `evolution.tree.role_visibility:RoleVisibilityView` → `evolution.tree.roles.role_visibility:RoleVisibilityView` | |
| `evolution.tree.role_visibility:visible_paths` → `evolution.tree.roles.role_visibility:visible_paths` | |
| `evolution.tree.role_visibility:visible_observation` → `evolution.tree.roles.role_visibility:visible_observation` | |
| `evolution.tree.chat_history:CONTINUATION_FIELDS` → `evolution.tree.roles.chat_history:CONTINUATION_FIELDS` | |
| `evolution.tree.chat_history:_ASSISTANT_FIELDS` → `evolution.tree.roles.chat_history:_ASSISTANT_FIELDS` | |
| `evolution.tree.chat_history:ChatHistoryError` → `evolution.tree.roles.chat_history:ChatHistoryError` | |
| `evolution.tree.chat_history:_fields` → `evolution.tree.roles.chat_history:_fields` | |
| `evolution.tree.chat_history:_tool_call` → `evolution.tree.roles.chat_history:_tool_call` | |
| `evolution.tree.chat_history:_call_ids` → `evolution.tree.roles.chat_history:_call_ids` | |
| `evolution.tree.chat_history:assistant_message` → `evolution.tree.roles.chat_history:assistant_message` | |
| `evolution.tree.chat_history:tool_exchange` → `evolution.tree.roles.chat_history:tool_exchange` | |
| `evolution.tree.chat_history:message_units` → `evolution.tree.roles.chat_history:message_units` | |
| `evolution.tree.editor_workspace:MAX_NOTE_BYTES` → `evolution.tree.roles.workspace:MAX_NOTE_BYTES` | |
| `evolution.tree.editor_workspace:PROJECTED_FILE_MODE` → `evolution.tree.roles.workspace:PROJECTED_FILE_MODE` | |
| `evolution.tree.editor_workspace:MAX_CAPTURE_ENTRIES` → `evolution.tree.roles.workspace:MAX_CAPTURE_ENTRIES` | |
| `evolution.tree.editor_workspace:MAX_CAPTURE_BYTES` → `evolution.tree.roles.workspace:MAX_CAPTURE_BYTES` | |
| `evolution.tree.editor_workspace:ROLE_PROJECTION_VERSION` → `evolution.tree.roles.workspace:ROLE_PROJECTION_VERSION` | |
| `evolution.tree.editor_workspace:_ROOT` → `evolution.tree.roles.workspace:_ROOT` | |
| `evolution.tree.editor_workspace:_EXECUTABLE_BITS` → `evolution.tree.roles.workspace:_EXECUTABLE_BITS` | |
| `evolution.tree.editor_workspace:_UNSAFE_FILE_BITS` → `evolution.tree.roles.workspace:_UNSAFE_FILE_BITS` | |
| `evolution.tree.editor_workspace:_UNSAFE_DIRECTORY_BITS` → `evolution.tree.roles.workspace:_UNSAFE_DIRECTORY_BITS` | |
| `evolution.tree.editor_workspace:_READ_CHUNK` → `evolution.tree.roles.workspace:_READ_CHUNK` | |
| `evolution.tree.editor_workspace:_NOT_A_REGULAR_FILE_ERRNOS` → `evolution.tree.execution.private_files:_NOT_A_REGULAR_FILE_ERRNOS` | |
| `evolution.tree.editor_workspace:WorkspaceError` → `evolution.tree.execution.private_files:WorkspaceError` | |
| `evolution.tree.editor_workspace:WorkspaceChanged` → `evolution.tree.roles.workspace:WorkspaceChanged` | |
| `evolution.tree.editor_workspace:ControlCollision` → `evolution.tree.roles.workspace:ControlCollision` | |
| `evolution.tree.editor_workspace:ActorViolation` → `evolution.tree.roles.workspace:ActorViolation` | |
| `evolution.tree.editor_workspace:BudgetViolation` → `evolution.tree.roles.workspace:BudgetViolation` | |
| `evolution.tree.editor_workspace:WorkspaceAnchor` → `evolution.tree.roles.workspace:WorkspaceAnchor` | |
| `evolution.tree.editor_workspace:Budgets` → `evolution.tree.roles.workspace:Budgets` | |
| `evolution.tree.editor_workspace:FileState` → `evolution.tree.roles.workspace:FileState` | |
| `evolution.tree.editor_workspace:Capture` → `evolution.tree.roles.workspace:Capture` | |
| `evolution.tree.editor_workspace:Change` → `evolution.tree.roles.workspace:Change` | |
| `evolution.tree.editor_workspace:Projection` → `evolution.tree.roles.workspace:Projection` | |
| `evolution.tree.editor_workspace:matches_rule` → `evolution.tree.roles.workspace:matches_rule` | |
| `evolution.tree.editor_workspace:relative_rule` → `evolution.tree.roles.workspace:relative_rule` | |
| `evolution.tree.editor_workspace:relative_candidate_path` → `evolution.tree.roles.workspace:relative_candidate_path` | |
| `evolution.tree.editor_workspace:workspace_anchor` → `evolution.tree.roles.workspace:workspace_anchor` | |
| `evolution.tree.editor_workspace:assert_anchored` → `evolution.tree.roles.workspace:assert_anchored` | |
| `evolution.tree.editor_workspace:admit_workspace` → `evolution.tree.roles.workspace:admit_workspace` | |
| `evolution.tree.editor_workspace:capture_tree` → `evolution.tree.roles.workspace:capture_tree` | |
| `evolution.tree.editor_workspace:visible_capture` → `evolution.tree.roles.workspace:visible_capture` | |
| `evolution.tree.editor_workspace:role_projection_digest` → `evolution.tree.roles.workspace:role_projection_digest` | |
| `evolution.tree.editor_workspace:project` → `evolution.tree.roles.workspace:project` | |
| `evolution.tree.editor_workspace:freeze_projection` → `evolution.tree.roles.workspace:freeze_projection` | |
| `evolution.tree.editor_workspace:admit` → `evolution.tree.roles.workspace:admit` | |
| `evolution.tree.editor_workspace:changed_file_paths` → `evolution.tree.roles.workspace:changed_file_paths` | |
| `evolution.tree.editor_workspace:_counted_paths` → `evolution.tree.roles.workspace:_counted_paths` | |
| `evolution.tree.editor_workspace:_hidden_collision` → `evolution.tree.roles.workspace:_hidden_collision` | |
| `evolution.tree.editor_workspace:publish` → `evolution.tree.roles.workspace:publish` | |
| `evolution.tree.editor_workspace:discard` → `evolution.tree.roles.workspace:discard` | |
| `evolution.tree.editor_workspace:discard_directory` → `evolution.tree.roles.workspace:discard_directory` | |
| `evolution.tree.editor_workspace:_REMOVAL_DESCRIPTORS` → `evolution.tree.roles.workspace:_REMOVAL_DESCRIPTORS` | |
| `evolution.tree.editor_workspace:_RemovalFrame` → `evolution.tree.roles.workspace:_RemovalFrame` | |
| `evolution.tree.editor_workspace:_remove_contents` → `evolution.tree.roles.workspace:_remove_contents` | |
| `evolution.tree.editor_workspace:self_check` → `evolution.tree.roles.workspace:self_check` | |
| `evolution.tree.editor_workspace:AttemptLog` → `evolution.tree.roles.workspace:AttemptLog` | |
| `evolution.tree.editor_workspace:open_attempt_log` → `evolution.tree.roles.workspace:open_attempt_log` | |
| `evolution.tree.editor_workspace:open_private_file` → `evolution.tree.execution.private_files:open_private_file` | |
| `evolution.tree.editor_workspace:bounded_note` → `evolution.tree.roles.workspace:bounded_note` | |
| `evolution.tree.editor_workspace:_directory_identity` → `evolution.tree.roles.workspace:_directory_identity` | |
| `evolution.tree.editor_workspace:_has_identity` → `evolution.tree.roles.workspace:_has_identity` | |
| `evolution.tree.editor_workspace:_file_identity` → `evolution.tree.roles.workspace:_file_identity` | |
| `evolution.tree.editor_workspace:_writable` → `evolution.tree.roles.workspace:_writable` | |
| `evolution.tree.editor_workspace:_is_control` → `evolution.tree.roles.workspace:_is_control` | |
| `evolution.tree.editor_workspace:_without_control` → `evolution.tree.roles.workspace:_without_control` | |
| `evolution.tree.editor_workspace:_control_subtree` → `evolution.tree.roles.workspace:_control_subtree` | |
| `evolution.tree.editor_workspace:_normalized` → `evolution.tree.roles.workspace:_normalized` | |
| `evolution.tree.editor_workspace:_file_changes` → `evolution.tree.roles.workspace:_file_changes` | |
| `evolution.tree.editor_workspace:_materialize` → `evolution.tree.roles.workspace:_materialize` | |
| `evolution.tree.editor_workspace:_replace_tree` → `evolution.tree.roles.workspace:_replace_tree` | |
| `evolution.tree.editor_seal:CHAT_TOOL_SCHEMA` → `evolution.tree.roles.seals:CHAT_TOOL_SCHEMA` | |
| `evolution.tree.editor_seal:CHAT_TOOL_SCHEMA_V16` → `evolution.tree.roles.seals:CHAT_TOOL_SCHEMA_V16` | |
| `evolution.tree.editor_seal:SealChanged` → `evolution.tree.roles.seals:SealChanged` | |
| `evolution.tree.editor_seal:FileSeal` → `evolution.tree.roles.seals:FileSeal` | |
| `evolution.tree.editor_seal:seal_prompt` → `evolution.tree.roles.seals:seal_prompt` | |
| `evolution.tree.editor_seal:seal_program` → `evolution.tree.roles.seals:seal_program` | |
| `evolution.tree.editor_seal:section_payload` → `evolution.tree.roles.seals:section_payload` | |
| `evolution.tree.editor_seal:chat_identity` → `evolution.tree.roles.seals:chat_identity` | |
| `evolution.tree.editor_seal:agent_identity` → `evolution.tree.roles.seals:agent_identity` | |
| `evolution.tree.editor_seal:chat_reflector_identity` → `evolution.tree.roles.seals:chat_reflector_identity` | |
| `evolution.tree.editor_seal:agent_reflector_identity` → `evolution.tree.roles.seals:agent_reflector_identity` | |
| `evolution.tree.editor_seal:chat_tool_schema_for` → `evolution.tree.roles.seals:chat_tool_schema_for` | |
| `evolution.tree.reflection_reader:MAX_ARGUMENT_BYTES` → `evolution.tree.roles.reflection_reader:MAX_ARGUMENT_BYTES` | |
| `evolution.tree.reflection_reader:MAX_RESPONSE_BYTES` → `evolution.tree.roles.reflection_reader:MAX_RESPONSE_BYTES` | |
| `evolution.tree.reflection_reader:MAX_STREAM_FRAME_BYTES` → `evolution.tree.roles.reflection_reader:MAX_STREAM_FRAME_BYTES` | |
| `evolution.tree.reflection_reader:MAX_SUMMARY_ENTRIES` → `evolution.tree.roles.reflection_reader:MAX_SUMMARY_ENTRIES` | |
| `evolution.tree.reflection_reader:MAX_SUMMARY_BYTES` → `evolution.tree.roles.reflection_reader:MAX_SUMMARY_BYTES` | |
| `evolution.tree.reflection_reader:BODY_CACHE_BYTES` → `evolution.tree.roles.reflection_reader:BODY_CACHE_BYTES` | |
| `evolution.tree.reflection_reader:__all__` → `evolution.tree.roles.reflection_reader:__all__` | |
| `evolution.tree.reflection_reader:ReaderError` → `evolution.tree.roles.reflection_reader:ReaderError` | |
| `evolution.tree.reflection_reader:ReaderArgumentError` → `evolution.tree.roles.reflection_reader:ReaderArgumentError` | |
| `evolution.tree.reflection_reader:ReaderIntegrityError` → `evolution.tree.roles.reflection_reader:ReaderIntegrityError` | |
| `evolution.tree.reflection_reader:ActivitySummary` → `evolution.tree.roles.reflection_reader:ActivitySummary` | |
| `evolution.tree.reflection_reader:ByteLruCache` → `evolution.tree.roles.reflection_reader:ByteLruCache` | |
| `evolution.tree.reflection_reader:Handler` → `evolution.tree.roles.reflection_reader:Handler` | |
| `evolution.tree.reflection_reader:IntegrityCheck` → `evolution.tree.roles.reflection_reader:IntegrityCheck` | |
| `evolution.tree.reflection_reader:FrozenReflectionView` → `evolution.tree.roles.reflection_reader:FrozenReflectionView` | |
| `evolution.tree.reflection_reader:ClientDescriptor` → `evolution.tree.roles.reflection_reader:ClientDescriptor` | |
| `evolution.tree.reflection_reader:ReflectionReadSession` → `evolution.tree.roles.reflection_reader:ReflectionReadSession` | |
| `evolution.tree.reflection_reader:_LoopbackServer` → `evolution.tree.roles.reflection_reader:_LoopbackServer` | |
| `evolution.tree.reflection_reader:_RequestHandler` → `evolution.tree.roles.reflection_reader:_RequestHandler` | |
| `evolution.tree.reflection_reader:_valid_operation` → `evolution.tree.roles.reflection_reader:_valid_operation` | |
| `evolution.tree.reflection_reader:_valid_operation_id` → `evolution.tree.roles.reflection_reader:_valid_operation_id` | |
| `evolution.tree.reflection_reader:_reject_routing_arguments` → `evolution.tree.roles.reflection_reader:_reject_routing_arguments` | |
| `evolution.tree.reflection_reader:_activity_fields` → `evolution.tree.roles.reflection_reader:_activity_fields` | |
| `evolution.tree.reflection_reader:_json_bytes` → `evolution.tree.roles.reflection_reader:_json_bytes` | |
| `evolution.tree.reflection_reader:_summary_size` → `evolution.tree.roles.reflection_reader:_summary_size` | |
| `evolution.tree.reflection_reader:_digest_or_empty` → `evolution.tree.roles.reflection_reader:_digest_or_empty` | |
| `evolution.tree.reflection_read_client:__all__` → `evolution.tree.roles.reflection_read_client:__all__` | |
| `evolution.tree.reflection_read_client:call` → `evolution.tree.roles.reflection_read_client:call` | |
| `evolution.tree.reflection_read_client:stream` → `evolution.tree.roles.reflection_read_client:stream` | |
| `evolution.tree.reflection_read_client:_request` → `evolution.tree.roles.reflection_read_client:_request` | |
| `evolution.tree.reflection_read_client:_NoRedirect` → `evolution.tree.roles.reflection_read_client:_NoRedirect` | |
| `evolution.tree.reflection_read_client:_validate_url` → `evolution.tree.roles.reflection_read_client:_validate_url` | |
| `evolution.tree.reflection_read_client:_read_bounded` → `evolution.tree.roles.reflection_read_client:_read_bounded` | |
| `evolution.tree.reflection_read_client:main` → `evolution.tree.roles.reflection_read_client:main` | |
| `evolution.tree.reflection_role_helpers:PROBE_CALL_LOG_NAME` → `evolution.tree.roles.reflection_role_helpers:PROBE_CALL_LOG_NAME` | |
| `evolution.tree.reflection_role_helpers:MAX_PROBE_CALL_LOG_BYTES` → `evolution.tree.roles.reflection_role_helpers:MAX_PROBE_CALL_LOG_BYTES` | |
| `evolution.tree.reflection_role_helpers:MAX_PROBE_CALL_LOG_ENTRIES` → `evolution.tree.roles.reflection_role_helpers:MAX_PROBE_CALL_LOG_ENTRIES` | |
| `evolution.tree.reflection_role_helpers:read_probe_call_log` → `evolution.tree.roles.reflection_role_helpers:read_probe_call_log` | |
| `evolution.tree.reflection_role_helpers:_METHODS` → `evolution.tree.roles.reflection_role_helpers:_METHODS` | |
| `evolution.tree.reflection_role_helpers:_CLIENT` → `evolution.tree.roles.reflection_role_helpers:_CLIENT` | |
| `evolution.tree.reflection_role_helpers:RoleHelpers` → `evolution.tree.roles.reflection_role_helpers:RoleHelpers` | |
| `evolution.tree.reflection_role_helpers:install_helpers` → `evolution.tree.roles.reflection_role_helpers:install_helpers` | |
| `evolution.tree.reflection_role_helpers:probe_log_secrets` → `evolution.tree.roles.reflection_role_helpers:probe_log_secrets` | |
| `evolution.tree.reflection_role_helpers:probe_opening` → `evolution.tree.roles.reflection_role_helpers:probe_opening` | |
| `evolution.tree.reflection_role_helpers:_PROBE_LAUNCHER` → `evolution.tree.roles.reflection_role_helpers:_PROBE_LAUNCHER` | |
| `evolution.tree.reflection_role_helpers:install_probe_helper` → `evolution.tree.roles.reflection_role_helpers:install_probe_helper` | |
| `evolution.tree.reflection_role_helpers:groups_without_probe_refs` → `evolution.tree.roles.reflection_role_helpers:groups_without_probe_refs` | |
| `evolution.tree.environment_docs:ENVIRONMENT_DOCS_ROOT` → `evolution.tree.roles.environment_docs:ENVIRONMENT_DOCS_ROOT` | |
| `evolution.tree.environment_docs:HEADER` → `evolution.tree.roles.environment_docs:HEADER` | |
| `evolution.tree.environment_docs:__all__` → `evolution.tree.roles.environment_docs:__all__` | |
| `evolution.tree.environment_docs:EnvironmentDocsError` → `evolution.tree.roles.environment_docs:EnvironmentDocsError` | |
| `evolution.tree.environment_docs:EnvironmentDocs` → `evolution.tree.roles.environment_docs:EnvironmentDocs` | |
| `evolution.tree.environment_docs:_collect` → `evolution.tree.roles.environment_docs:_collect` | |
| `evolution.tree.environment_docs:_relative` → `evolution.tree.roles.environment_docs:_relative` | |
| `evolution.tree.environment_docs:_render` → `evolution.tree.roles.environment_docs:_render` | |
| `evolution.tree.environment_docs:render_environment_docs` → `evolution.tree.roles.environment_docs:render_environment_docs` | |
| `evolution.tree.environment_docs:write_snapshot` → `evolution.tree.roles.environment_docs:write_snapshot` | |
| `evolution.tree.environment_docs:snapshot_state` → `evolution.tree.roles.environment_docs:snapshot_state` | |
| `evolution.tree.environment_docs:_V2_DIGEST_VERSION` → `evolution.tree.roles.environment_docs:_V2_DIGEST_VERSION` | |
| `evolution.tree.environment_docs:_V2_DOCUMENT_ID_VERSION` → `evolution.tree.roles.environment_docs:_V2_DOCUMENT_ID_VERSION` | |
| `evolution.tree.environment_docs:_V2_MAX_FILES` → `evolution.tree.roles.environment_docs:_V2_MAX_FILES` | |
| `evolution.tree.environment_docs:_V2_MAX_BYTES` → `evolution.tree.roles.environment_docs:_V2_MAX_BYTES` | |
| `evolution.tree.environment_docs:_V2_CATALOG_PAGE` → `evolution.tree.roles.environment_docs:_V2_CATALOG_PAGE` | |
| `evolution.tree.environment_docs:_V2_DOCUMENT_PAGE_CHARS` → `evolution.tree.roles.environment_docs:_V2_DOCUMENT_PAGE_CHARS` | |
| `evolution.tree.environment_docs:_V2Document` → `evolution.tree.roles.environment_docs:_V2Document` | |
| `evolution.tree.environment_docs:_V2DocumentSnapshot` → `evolution.tree.roles.environment_docs:_V2DocumentSnapshot` | |
| `evolution.tree.environment_docs:_V2DocumentCatalogEntry` → `evolution.tree.roles.environment_docs:_V2DocumentCatalogEntry` | |
| `evolution.tree.environment_docs:_V2DocumentCatalogPage` → `evolution.tree.roles.environment_docs:_V2DocumentCatalogPage` | |
| `evolution.tree.environment_docs:_V2DocumentPage` → `evolution.tree.roles.environment_docs:_V2DocumentPage` | |
| `evolution.tree.environment_docs:_v2_error` → `evolution.tree.roles.environment_docs:_v2_error` | |
| `evolution.tree.environment_docs:_v2_relative` → `evolution.tree.roles.environment_docs:_v2_relative` | |
| `evolution.tree.environment_docs:_v2_source_files` → `evolution.tree.roles.environment_docs:_v2_source_files` | |
| `evolution.tree.environment_docs:_v2_snapshot_digest` → `evolution.tree.roles.environment_docs:_v2_snapshot_digest` | |
| `evolution.tree.environment_docs:_v2_document_id` → `evolution.tree.roles.environment_docs:_v2_document_id` | |
| `evolution.tree.environment_docs:_v2_from_files` → `evolution.tree.roles.environment_docs:_v2_from_files` | |
| `evolution.tree.environment_docs:_v2_create_document_snapshot` → `evolution.tree.roles.environment_docs:_v2_create_document_snapshot` | |
| `evolution.tree.environment_docs:_v2_existing_parent` → `evolution.tree.roles.environment_docs:_v2_existing_parent` | |
| `evolution.tree.environment_docs:_v2_require_free_space` → `evolution.tree.roles.environment_docs:_v2_require_free_space` | |
| `evolution.tree.environment_docs:_v2_fsync_tree` → `evolution.tree.roles.environment_docs:_v2_fsync_tree` | |
| `evolution.tree.environment_docs:_v2_remove_tree` → `evolution.tree.roles.environment_docs:_v2_remove_tree` | |
| `evolution.tree.environment_docs:_v2_write_document_snapshot` → `evolution.tree.roles.environment_docs:_v2_write_document_snapshot` | |
| `evolution.tree.environment_docs:_v2_load_document_snapshot` → `evolution.tree.roles.environment_docs:_v2_load_document_snapshot` | |
| `evolution.tree.environment_docs:_v2_check_document_snapshot` → `evolution.tree.roles.environment_docs:_v2_check_document_snapshot` | |
| `evolution.tree.environment_docs:_v2_repair_document_snapshot` → `evolution.tree.roles.environment_docs:_v2_repair_document_snapshot` | |
| `evolution.tree.environment_docs:_v2_catalog_page` → `evolution.tree.roles.environment_docs:_v2_catalog_page` | |
| `evolution.tree.environment_docs:_v2_catalog_response_bytes` → `evolution.tree.roles.environment_docs:_v2_catalog_response_bytes` | |
| `evolution.tree.environment_docs:_v2_read_document_page` → `evolution.tree.roles.environment_docs:_v2_read_document_page` | |
| `evolution.tree.grouping_runtime:__all__` → `evolution.tree.roles.grouping.grouping_runtime:__all__` | |
| `evolution.tree.grouping_runtime:GroupingExecutionError` → `evolution.tree.roles.role_failures:GroupingExecutionError` | |
| `evolution.tree.grouping_runtime:family_membership_for` → `evolution.tree.roles.grouping.grouping_runtime:family_membership_for` | |
| `evolution.tree.grouping_runtime:_frozen_json` → `evolution.tree.roles.grouping.grouping_runtime:_frozen_json` | |
| `evolution.tree.grouping_runtime:SelectionRuntimeConfig` → `evolution.tree.roles.grouping.grouping_runtime:SelectionRuntimeConfig` | |
| `evolution.tree.grouping_runtime:AdaptiveCampaignRuntimeConfig` → `evolution.tree.roles.grouping.grouping_runtime:AdaptiveCampaignRuntimeConfig` | |
| `evolution.tree.grouping_runtime:_role_exception_class` → `evolution.tree.roles.grouping.grouping_runtime:_role_exception_class` | |
| `evolution.tree.grouping_runtime:GroupingRuntime` → `evolution.tree.roles.grouping.grouping_runtime:GroupingRuntime` | |
| `evolution.tree.model_grouping:__all__` → `evolution.tree.roles.grouping.model_grouping:__all__` | |
| `evolution.tree.model_grouping:_PROVENANCE_UNAVAILABLE` → `evolution.tree.roles.grouping.model_grouping:_PROVENANCE_UNAVAILABLE` | |
| `evolution.tree.model_grouping:REPRESENTATIVE_CHARS_START` → `evolution.tree.roles.grouping.model_grouping:REPRESENTATIVE_CHARS_START` | |
| `evolution.tree.model_grouping:REPRESENTATIVE_CHARS_FLOOR` → `evolution.tree.roles.grouping.model_grouping:REPRESENTATIVE_CHARS_FLOOR` | |
| `evolution.tree.model_grouping:BUNDLE_OVERHEAD` → `evolution.tree.roles.grouping.model_grouping:BUNDLE_OVERHEAD` | |
| `evolution.tree.model_grouping:_TRUNCATION_MARKER` → `evolution.tree.roles.grouping.model_grouping:_TRUNCATION_MARKER` | |
| `evolution.tree.model_grouping:ObservationRow` → `evolution.tree.roles.grouping.model_grouping:ObservationRow` | |
| `evolution.tree.model_grouping:CaseProfile` → `evolution.tree.roles.grouping.model_grouping:CaseProfile` | |
| `evolution.tree.model_grouping:CaseBundle` → `evolution.tree.roles.grouping.model_grouping:CaseBundle` | |
| `evolution.tree.model_grouping:InputTooLarge` → `evolution.tree.roles.grouping.model_grouping:InputTooLarge` | |
| `evolution.tree.model_grouping:_text_key` → `evolution.tree.roles.grouping.model_grouping:_text_key` | |
| `evolution.tree.model_grouping:_finite_metric` → `evolution.tree.roles.grouping.model_grouping:_finite_metric` | |
| `evolution.tree.model_grouping:_observation_row` → `evolution.tree.roles.grouping.model_grouping:_observation_row` | |
| `evolution.tree.model_grouping:_terminal_counts` → `evolution.tree.roles.grouping.model_grouping:_terminal_counts` | |
| `evolution.tree.model_grouping:_failed_criterion_counts` → `evolution.tree.roles.grouping.model_grouping:_failed_criterion_counts` | |
| `evolution.tree.model_grouping:_metric_values` → `evolution.tree.roles.grouping.model_grouping:_metric_values` | |
| `evolution.tree.model_grouping:_select_representatives` → `evolution.tree.roles.grouping.model_grouping:_select_representatives` | |
| `evolution.tree.model_grouping:_select_contrast` → `evolution.tree.roles.grouping.model_grouping:_select_contrast` | |
| `evolution.tree.model_grouping:build_case_profiles` → `evolution.tree.roles.grouping.model_grouping:build_case_profiles` | |
| `evolution.tree.model_grouping:_compact_row` → `evolution.tree.roles.grouping.model_grouping:_compact_row` | |
| `evolution.tree.model_grouping:_render_text` → `evolution.tree.roles.grouping.model_grouping:_render_text` | |
| `evolution.tree.model_grouping:_representative_payload` → `evolution.tree.roles.grouping.model_grouping:_representative_payload` | |
| `evolution.tree.model_grouping:build_bundle` → `evolution.tree.roles.grouping.model_grouping:build_bundle` | |
| `evolution.tree.model_grouping:chunk_bundles` → `evolution.tree.roles.grouping.model_grouping:chunk_bundles` | |
| `evolution.tree.model_grouping:_TITLE_CHARS` → `evolution.tree.roles.grouping.model_grouping:_TITLE_CHARS` | |
| `evolution.tree.model_grouping:_HEADER_CHARS` → `evolution.tree.roles.grouping.model_grouping:_HEADER_CHARS` | |
| `evolution.tree.model_grouping:_PRIORITY_CHARS` → `evolution.tree.roles.grouping.model_grouping:_PRIORITY_CHARS` | |
| `evolution.tree.model_grouping:_VARIANT_CHARS` → `evolution.tree.roles.grouping.model_grouping:_VARIANT_CHARS` | |
| `evolution.tree.model_grouping:_MAX_VARIANTS` → `evolution.tree.roles.grouping.model_grouping:_MAX_VARIANTS` | |
| `evolution.tree.model_grouping:_MAX_CITATIONS` → `evolution.tree.roles.grouping.model_grouping:_MAX_CITATIONS` | |
| `evolution.tree.model_grouping:_CITATION_CHARS` → `evolution.tree.roles.grouping.model_grouping:_CITATION_CHARS` | |
| `evolution.tree.model_grouping:_MAX_METRICS` → `evolution.tree.roles.grouping.model_grouping:_MAX_METRICS` | |
| `evolution.tree.model_grouping:_METRIC_NAME_CHARS` → `evolution.tree.roles.grouping.model_grouping:_METRIC_NAME_CHARS` | |
| `evolution.tree.model_grouping:_PROVENANCE_CHARS` → `evolution.tree.roles.grouping.model_grouping:_PROVENANCE_CHARS` | |
| `evolution.tree.model_grouping:_DESCRIPTION_CHARS` → `evolution.tree.roles.grouping.model_grouping:_DESCRIPTION_CHARS` | |
| `evolution.tree.model_grouping:_TOKEN_CHARS` → `evolution.tree.roles.grouping.model_grouping:_TOKEN_CHARS` | |
| `evolution.tree.model_grouping:_MAX_DEFECTS` → `evolution.tree.roles.grouping.model_grouping:_MAX_DEFECTS` | |
| `evolution.tree.model_grouping:_REQUIRED_KEYS` → `evolution.tree.roles.grouping.model_grouping:_REQUIRED_KEYS` | |
| `evolution.tree.model_grouping:_OPTIONAL_KEYS` → `evolution.tree.roles.grouping.model_grouping:_OPTIONAL_KEYS` | |
| `evolution.tree.model_grouping:_HEADER_BOUNDS` → `evolution.tree.roles.grouping.model_grouping:_HEADER_BOUNDS` | |
| `evolution.tree.model_grouping:_REDUCE_HEADER_KEYS` → `evolution.tree.roles.grouping.model_grouping:_REDUCE_HEADER_KEYS` | |
| `evolution.tree.model_grouping:_SAFE_TOKEN` → `evolution.tree.roles.grouping.model_grouping:_SAFE_TOKEN` | |
| `evolution.tree.model_grouping:_FENCE` → `evolution.tree.roles.grouping.model_grouping:_FENCE` | |
| `evolution.tree.model_grouping:ModelGroup` → `evolution.tree.roles.grouping.model_grouping:ModelGroup` | |
| `evolution.tree.model_grouping:ParsedResponse` → `evolution.tree.roles.grouping.model_grouping:ParsedResponse` | |
| `evolution.tree.model_grouping:FallbackOutcome` → `evolution.tree.roles.grouping.model_grouping:FallbackOutcome` | |
| `evolution.tree.model_grouping:_token` → `evolution.tree.roles.grouping.model_grouping:_token` | |
| `evolution.tree.model_grouping:_named` → `evolution.tree.roles.grouping.model_grouping:_named` | |
| `evolution.tree.model_grouping:_reject_duplicate_keys` → `evolution.tree.roles.grouping.model_grouping:_reject_duplicate_keys` | |
| `evolution.tree.model_grouping:_reject_constant` → `evolution.tree.roles.grouping.model_grouping:_reject_constant` | |
| `evolution.tree.model_grouping:_DECODER` → `evolution.tree.roles.grouping.model_grouping:_DECODER` | |
| `evolution.tree.model_grouping:_extract_object` → `evolution.tree.roles.grouping.model_grouping:_extract_object` | |
| `evolution.tree.model_grouping:_bounded_text` → `evolution.tree.roles.grouping.model_grouping:_bounded_text` | |
| `evolution.tree.model_grouping:_parse_metrics` → `evolution.tree.roles.grouping.model_grouping:_parse_metrics` | |
| `evolution.tree.model_grouping:_parse_citations` → `evolution.tree.roles.grouping.model_grouping:_parse_citations` | |
| `evolution.tree.model_grouping:_ChunkContext` → `evolution.tree.roles.grouping.model_grouping:_ChunkContext` | |
| `evolution.tree.model_grouping:_parse_members` → `evolution.tree.roles.grouping.model_grouping:_parse_members` | |
| `evolution.tree.model_grouping:_check_keys` → `evolution.tree.roles.grouping.model_grouping:_check_keys` | |
| `evolution.tree.model_grouping:_check_header` → `evolution.tree.roles.grouping.model_grouping:_check_header` | |
| `evolution.tree.model_grouping:_check_provenance` → `evolution.tree.roles.grouping.model_grouping:_check_provenance` | |
| `evolution.tree.model_grouping:_parse_group` → `evolution.tree.roles.grouping.model_grouping:_parse_group` | |
| `evolution.tree.model_grouping:_coverage_defects` → `evolution.tree.roles.grouping.model_grouping:_coverage_defects` | |
| `evolution.tree.model_grouping:_bounded_defects` → `evolution.tree.roles.grouping.model_grouping:_bounded_defects` | |
| `evolution.tree.model_grouping:parse_map_response` → `evolution.tree.roles.grouping.model_grouping:parse_map_response` | |
| `evolution.tree.model_grouping:covered` → `evolution.tree.roles.grouping.model_grouping:covered` | |
| `evolution.tree.model_grouping:_admission_key` → `evolution.tree.roles.grouping.model_grouping:_admission_key` | |
| `evolution.tree.model_grouping:_admission_order` → `evolution.tree.roles.grouping.model_grouping:_admission_order` | |
| `evolution.tree.model_grouping:_keep_members` → `evolution.tree.roles.grouping.model_grouping:_keep_members` | |
| `evolution.tree.model_grouping:resolve_overlaps` → `evolution.tree.roles.grouping.model_grouping:resolve_overlaps` | |
| `evolution.tree.model_grouping:_parse_merge_header` → `evolution.tree.roles.grouping.model_grouping:_parse_merge_header` | |
| `evolution.tree.model_grouping:_parse_merge` → `evolution.tree.roles.grouping.model_grouping:_parse_merge` | |
| `evolution.tree.model_grouping:parse_reduce_response` → `evolution.tree.roles.grouping.model_grouping:parse_reduce_response` | |
| `evolution.tree.model_grouping:_unique` → `evolution.tree.roles.grouping.model_grouping:_unique` | |
| `evolution.tree.model_grouping:_merge_groups` → `evolution.tree.roles.grouping.model_grouping:_merge_groups` | |
| `evolution.tree.model_grouping:AppliedMerges` → `evolution.tree.roles.grouping.model_grouping:AppliedMerges` | |
| `evolution.tree.model_grouping:apply_merges` → `evolution.tree.roles.grouping.model_grouping:apply_merges` | |
| `evolution.tree.model_grouping:_TRIMMED_TITLE_CHARS` → `evolution.tree.roles.grouping.model_grouping:_TRIMMED_TITLE_CHARS` | |
| `evolution.tree.model_grouping:_TRIMMED_FIELD_CHARS` → `evolution.tree.roles.grouping.model_grouping:_TRIMMED_FIELD_CHARS` | |
| `evolution.tree.model_grouping:_TRIMMED_VARIANT_CHARS` → `evolution.tree.roles.grouping.model_grouping:_TRIMMED_VARIANT_CHARS` | |
| `evolution.tree.model_grouping:_clip` → `evolution.tree.roles.grouping.model_grouping:_clip` | |
| `evolution.tree.model_grouping:_description` → `evolution.tree.roles.grouping.model_grouping:_description` | |
| `evolution.tree.model_grouping:_check_features` → `evolution.tree.roles.grouping.model_grouping:_check_features` | |
| `evolution.tree.model_grouping:_field_assignments` → `evolution.tree.roles.grouping.model_grouping:_field_assignments` | |
| `evolution.tree.model_grouping:_Projection` → `evolution.tree.roles.grouping.model_grouping:_Projection` | |
| `evolution.tree.model_grouping:_eligible` → `evolution.tree.roles.grouping.model_grouping:_eligible` | |
| `evolution.tree.model_grouping:_project_model_groups` → `evolution.tree.roles.grouping.model_grouping:_project_model_groups` | |
| `evolution.tree.model_grouping:_SparseMap` → `evolution.tree.roles.grouping.model_grouping:_SparseMap` | |
| `evolution.tree.model_grouping:_short_labels` → `evolution.tree.roles.grouping.model_grouping:_short_labels` | |
| `evolution.tree.model_grouping:_fit_descriptions` → `evolution.tree.roles.grouping.model_grouping:_fit_descriptions` | |
| `evolution.tree.model_grouping:_fit_variants` → `evolution.tree.roles.grouping.model_grouping:_fit_variants` | |
| `evolution.tree.model_grouping:_fit_sparse_maps` → `evolution.tree.roles.grouping.model_grouping:_fit_sparse_maps` | |
| `evolution.tree.model_grouping:project` → `evolution.tree.roles.grouping.model_grouping:project` | |
| `evolution.tree.model_grouping_runtime:__all__` → `evolution.tree.roles.grouping.model_grouping_runtime:__all__` | |
| `evolution.tree.model_grouping_runtime:MAP_LEAD` → `evolution.tree.roles.grouping.model_grouping_runtime:MAP_LEAD` | |
| `evolution.tree.model_grouping_runtime:REDUCE_LEAD` → `evolution.tree.roles.grouping.model_grouping_runtime:REDUCE_LEAD` | |
| `evolution.tree.model_grouping_runtime:_REDUCE_HEADING` → `evolution.tree.roles.grouping.model_grouping_runtime:_REDUCE_HEADING` | |
| `evolution.tree.model_grouping_runtime:_ROLE` → `evolution.tree.roles.grouping.model_grouping_runtime:_ROLE` | |
| `evolution.tree.model_grouping_runtime:REPAIR_RESERVE` → `evolution.tree.roles.grouping.model_grouping_runtime:REPAIR_RESERVE` | |
| `evolution.tree.model_grouping_runtime:DEFECTS_BOUND` → `evolution.tree.roles.grouping.model_grouping_runtime:DEFECTS_BOUND` | |
| `evolution.tree.model_grouping_runtime:_FRESH_NOTE` → `evolution.tree.roles.grouping.model_grouping_runtime:_FRESH_NOTE` | |
| `evolution.tree.model_grouping_runtime:_MAP_REPAIR` → `evolution.tree.roles.grouping.model_grouping_runtime:_MAP_REPAIR` | |
| `evolution.tree.model_grouping_runtime:_REDUCE_REPAIR` → `evolution.tree.roles.grouping.model_grouping_runtime:_REDUCE_REPAIR` | |
| `evolution.tree.model_grouping_runtime:ReduceState` → `evolution.tree.roles.grouping.model_grouping_runtime:ReduceState` | |
| `evolution.tree.model_grouping_runtime:Fallback` → `evolution.tree.roles.grouping.model_grouping_runtime:Fallback` | |
| `evolution.tree.model_grouping_runtime:ModelGroupingOutcome` → `evolution.tree.roles.grouping.model_grouping_runtime:ModelGroupingOutcome` | |
| `evolution.tree.model_grouping_runtime:split_prompt` → `evolution.tree.roles.grouping.model_grouping_runtime:split_prompt` | |
| `evolution.tree.model_grouping_runtime:new_grouping_client` → `evolution.tree.roles.grouping.model_grouping_runtime:new_grouping_client` | |
| `evolution.tree.model_grouping_runtime:_request_options` → `evolution.tree.roles.grouping.model_grouping_runtime:_request_options` | |
| `evolution.tree.model_grouping_runtime:_DeadlineElapsed` → `evolution.tree.roles.grouping.model_grouping_runtime:_DeadlineElapsed` | |
| `evolution.tree.model_grouping_runtime:_response_text` → `evolution.tree.roles.grouping.model_grouping_runtime:_response_text` | |
| `evolution.tree.model_grouping_runtime:_Session` → `evolution.tree.roles.grouping.model_grouping_runtime:_Session` | |
| `evolution.tree.model_grouping_runtime:_repair_messages` → `evolution.tree.roles.grouping.model_grouping_runtime:_repair_messages` | |
| `evolution.tree.model_grouping_runtime:_fits` → `evolution.tree.roles.grouping.model_grouping_runtime:_fits` | |
| `evolution.tree.model_grouping_runtime:_bounded_defects` → `evolution.tree.roles.grouping.model_grouping_runtime:_bounded_defects` | |
| `evolution.tree.model_grouping_runtime:_repair_request` → `evolution.tree.roles.grouping.model_grouping_runtime:_repair_request` | |
| `evolution.tree.model_grouping_runtime:_map_chunk` → `evolution.tree.roles.grouping.model_grouping_runtime:_map_chunk` | |
| `evolution.tree.model_grouping_runtime:_map_all` → `evolution.tree.roles.grouping.model_grouping_runtime:_map_all` | |
| `evolution.tree.model_grouping_runtime:_reduce_input` → `evolution.tree.roles.grouping.model_grouping_runtime:_reduce_input` | |
| `evolution.tree.model_grouping_runtime:_reduce` → `evolution.tree.roles.grouping.model_grouping_runtime:_reduce` | |
| `evolution.tree.model_grouping_runtime:_HEADER_FIELDS` → `evolution.tree.roles.grouping.model_grouping_runtime:_HEADER_FIELDS` | |
| `evolution.tree.model_grouping_runtime:_scrub` → `evolution.tree.roles.grouping.model_grouping_runtime:_scrub` | |
| `evolution.tree.model_grouping_runtime:_model_strings` → `evolution.tree.roles.grouping.model_grouping_runtime:_model_strings` | |
| `evolution.tree.model_grouping_runtime:_close` → `evolution.tree.roles.grouping.model_grouping_runtime:_close` | |
| `evolution.tree.model_grouping_runtime:_close_client` → `evolution.tree.roles.grouping.model_grouping_runtime:_close_client` | |
| `evolution.tree.model_grouping_runtime:_provider_failure` → `evolution.tree.roles.grouping.model_grouping_runtime:_provider_failure` | |
| `evolution.tree.model_grouping_runtime:admit_model_groups` → `evolution.tree.roles.grouping.model_grouping_runtime:admit_model_groups` | |
| `evolution.tree.model_grouping_runtime:run_model_grouping` → `evolution.tree.roles.grouping.model_grouping_runtime:run_model_grouping` | |
| `evolution.tree.agent_grouper:CONTROL_PREFIX` → `evolution.tree.roles.grouping.agent_grouper:CONTROL_PREFIX` | |
| `evolution.tree.agent_grouper:REQUEST_FILE` → `evolution.tree.roles.grouping.agent_grouper:REQUEST_FILE` | |
| `evolution.tree.agent_grouper:OPENING_FILE` → `evolution.tree.roles.grouping.agent_grouper:OPENING_FILE` | |
| `evolution.tree.agent_grouper:GROUPS_FILE` → `evolution.tree.roles.grouping.agent_grouper:GROUPS_FILE` | |
| `evolution.tree.agent_grouper:VALIDATION_FILE` → `evolution.tree.roles.grouping.agent_grouper:VALIDATION_FILE` | |
| `evolution.tree.agent_grouper:GROUPING_METHODS` → `evolution.tree.roles.grouping.agent_grouper:GROUPING_METHODS` | |
| `evolution.tree.agent_grouper:MAX_REQUEST_BYTES` → `evolution.tree.roles.grouping.agent_grouper:MAX_REQUEST_BYTES` | |
| `evolution.tree.agent_grouper:GroupingControlError` → `evolution.tree.roles.grouping.agent_grouper:GroupingControlError` | |
| `evolution.tree.agent_grouper:_Seal` → `evolution.tree.roles.grouping.agent_grouper:_Seal` | |
| `evolution.tree.agent_grouper:GroupingControlPlane` → `evolution.tree.roles.grouping.agent_grouper:GroupingControlPlane` | |
| `evolution.tree.agent_grouper:_directory_identity` → `evolution.tree.roles.grouping.agent_grouper:_directory_identity` | |
| `evolution.tree.agent_grouper:_write_stable` → `evolution.tree.roles.grouping.agent_grouper:_write_stable` | |
| `evolution.tree.agent_grouper:_read_regular` → `evolution.tree.roles.grouping.agent_grouper:_read_regular` | |
| `evolution.tree.agent_grouper:write_grouping_control` → `evolution.tree.roles.grouping.agent_grouper:write_grouping_control` | |
| `evolution.tree.agent_grouper:read_grouping_output` → `evolution.tree.roles.grouping.agent_grouper:read_grouping_output` | |
| `evolution.tree.agent_grouper:_CallLog` → `evolution.tree.roles.grouping.agent_grouper:_CallLog` | |
| `evolution.tree.agent_grouper:_OPENING` → `evolution.tree.roles.grouping.agent_grouper:_OPENING` | |
| `evolution.tree.agent_grouper:_REPAIR_OPENING` → `evolution.tree.roles.grouping.agent_grouper:_REPAIR_OPENING` | |
| `evolution.tree.agent_grouper:_CONTROL_CODES` → `evolution.tree.roles.grouping.agent_grouper:_CONTROL_CODES` | |
| `evolution.tree.agent_grouper:_refuse_secret` → `evolution.tree.roles.grouping.agent_grouper:_refuse_secret` | |
| `evolution.tree.agent_grouper:_write_validation` → `evolution.tree.roles.grouping.agent_grouper:_write_validation` | |
| `evolution.tree.agent_grouper:AgentCliGrouper` → `evolution.tree.roles.grouping.agent_grouper:AgentCliGrouper` | |
| `evolution.tree.reflect_grouping:MAX_REGISTRY_RECORDS` → `evolution.tree.roles.grouping.reflect_grouping:MAX_REGISTRY_RECORDS` | |
| `evolution.tree.reflect_grouping:MAX_GROUP_NUMBER` → `evolution.tree.roles.grouping.reflect_grouping:MAX_GROUP_NUMBER` | |
| `evolution.tree.reflect_grouping:MAX_RESULT_BYTES` → `evolution.tree.roles.grouping.reflect_grouping:MAX_RESULT_BYTES` | |
| `evolution.tree.reflect_grouping:MAX_SPARSE_MAP_BYTES` → `evolution.tree.roles.grouping.reflect_grouping:MAX_SPARSE_MAP_BYTES` | |
| `evolution.tree.reflect_grouping:MISSING` → `evolution.tree.execution.json_pointer:MISSING` | |
| `evolution.tree.reflect_grouping:_SOURCES` → `evolution.tree.roles.grouping.reflect_grouping:_SOURCES` | |
| `evolution.tree.reflect_grouping:_FEATURE_SOURCES` → `evolution.tree.roles.grouping.reflect_grouping:_FEATURE_SOURCES` | |
| `evolution.tree.reflect_grouping:_GROUPING_VERSION` → `evolution.tree.roles.grouping.reflect_grouping:_GROUPING_VERSION` | |
| `evolution.tree.reflect_grouping:GroupingAdmissionError` → `evolution.tree.roles.grouping.reflect_grouping:GroupingAdmissionError` | |
| `evolution.tree.reflect_grouping:_fail` → `evolution.tree.roles.grouping.reflect_grouping:_fail` | |
| `evolution.tree.reflect_grouping:_canonical_json` → `evolution.tree.roles.grouping.reflect_grouping:_canonical_json` | |
| `evolution.tree.reflect_grouping:_digest` → `evolution.tree.roles.grouping.reflect_grouping:_digest` | |
| `evolution.tree.reflect_grouping:_require_text` → `evolution.tree.roles.grouping.reflect_grouping:_require_text` | |
| `evolution.tree.reflect_grouping:GroupingRecord` → `evolution.tree.roles.grouping.reflect_grouping:GroupingRecord` | |
| `evolution.tree.reflect_grouping:GroupingRegistry` → `evolution.tree.roles.grouping.reflect_grouping:GroupingRegistry` | |
| `evolution.tree.reflect_grouping:build_registry` → `evolution.tree.roles.grouping.reflect_grouping:build_registry` | |
| `evolution.tree.reflect_grouping:FieldFeature` → `evolution.tree.roles.grouping.reflect_grouping:FieldFeature` | |
| `evolution.tree.reflect_grouping:_pointer_tokens` → `evolution.tree.execution.json_pointer:_pointer_tokens` | |
| `evolution.tree.reflect_grouping:DEFAULT_FIELD_FEATURES` → `evolution.tree.roles.grouping.reflect_grouping:DEFAULT_FIELD_FEATURES` | |
| `evolution.tree.reflect_grouping:pointer_value` → `evolution.tree.execution.json_pointer:pointer_value` | |
| `evolution.tree.reflect_grouping:typed_json_key` → `evolution.tree.roles.grouping.reflect_grouping:typed_json_key` | |
| `evolution.tree.reflect_grouping:_typed_json_key` → `evolution.tree.roles.grouping.reflect_grouping:_typed_json_key` | |
| `evolution.tree.reflect_grouping:GroupDescription` → `evolution.tree.roles.grouping.reflect_grouping:GroupDescription` | |
| `evolution.tree.reflect_grouping:GroupingResult` → `evolution.tree.roles.grouping.reflect_grouping:GroupingResult` | |
| `evolution.tree.reflect_grouping:Neighborhood` → `evolution.tree.roles.grouping.reflect_grouping:Neighborhood` | |
| `evolution.tree.reflect_grouping:AdmittedGrouping` → `evolution.tree.roles.grouping.reflect_grouping:AdmittedGrouping` | |
| `evolution.tree.reflect_grouping:_mapping_key` → `evolution.tree.roles.grouping.reflect_grouping:_mapping_key` | |
| `evolution.tree.reflect_grouping:_checked_sparse_bytes` → `evolution.tree.roles.grouping.reflect_grouping:_checked_sparse_bytes` | |
| `evolution.tree.reflect_grouping:_scan_strings` → `evolution.tree.roles.grouping.reflect_grouping:_scan_strings` | |
| `evolution.tree.reflect_grouping:_parse_descriptions` → `evolution.tree.roles.grouping.reflect_grouping:_parse_descriptions` | |
| `evolution.tree.reflect_grouping:_parse_variants` → `evolution.tree.roles.grouping.reflect_grouping:_parse_variants` | |
| `evolution.tree.reflect_grouping:_validate_result_shape` → `evolution.tree.roles.grouping.reflect_grouping:_validate_result_shape` | |
| `evolution.tree.reflect_grouping:admit_grouping_result` → `evolution.tree.roles.grouping.reflect_grouping:admit_grouping_result` | |
| `evolution.tree.reflect_grouping:builtin_grouping` → `evolution.tree.roles.grouping.reflect_grouping:builtin_grouping` | |
| `evolution.tree.reflect_grouping:Cohort` → `evolution.tree.roles.grouping.reflect_grouping:Cohort` | |
| `evolution.tree.reflect_grouping:cohorts_from_grouping` → `evolution.tree.roles.grouping.reflect_grouping:cohorts_from_grouping` | |
| `evolution.tree.reflect_grouping:PortionCohort` → `evolution.tree.roles.grouping.reflect_grouping:PortionCohort` | |
| `evolution.tree.reflect_grouping:PortionSelection` → `evolution.tree.roles.grouping.reflect_grouping:PortionSelection` | |
| `evolution.tree.reflect_grouping:_observation_value` → `evolution.tree.roles.grouping.reflect_grouping:_observation_value` | |
| `evolution.tree.reflect_grouping:_variant_signature` → `evolution.tree.roles.grouping.reflect_grouping:_variant_signature` | |
| `evolution.tree.reflect_grouping:_portion_for_cohort` → `evolution.tree.roles.grouping.reflect_grouping:_portion_for_cohort` | |
| `evolution.tree.reflect_grouping:select_portion` → `evolution.tree.roles.grouping.reflect_grouping:select_portion` | |
| `evolution.tree.agent_reflector:__all__` → `evolution.tree.roles.reflector.agent_reflector:__all__` | |
| `evolution.tree.agent_reflector:REFLECT_REQUEST_FILE` → `evolution.tree.roles.reflector.agent_reflector:REFLECT_REQUEST_FILE` | |
| `evolution.tree.agent_reflector:REFLECTION_FILE` → `evolution.tree.roles.reflector.agent_reflector:REFLECTION_FILE` | |
| `evolution.tree.agent_reflector:CONTROL_PREFIX` → `evolution.tree.roles.editor.agent_editor:CONTROL_PREFIX` | |
| `evolution.tree.agent_reflector:VALIDATION_FILE` → `evolution.tree.roles.reflector.agent_reflector:VALIDATION_FILE` | |
| `evolution.tree.agent_reflector:_EVIDENCE_DIR` → `evolution.tree.roles.reflector.agent_reflector:_EVIDENCE_DIR` | |
| `evolution.tree.agent_reflector:_EVIDENCE_INDEX` → `evolution.tree.roles.reflector.agent_reflector:_EVIDENCE_INDEX` | |
| `evolution.tree.agent_reflector:_DOCS_DIR` → `evolution.tree.roles.reflector.agent_reflector:_DOCS_DIR` | |
| `evolution.tree.agent_reflector:_DOCS_FILE` → `evolution.tree.roles.reflector.agent_reflector:_DOCS_FILE` | |
| `evolution.tree.agent_reflector:_STATUSES` → `evolution.tree.roles.reflector.agent_reflector:_STATUSES` | |
| `evolution.tree.agent_reflector:AgentCliReflector` → `evolution.tree.roles.reflector.agent_reflector:AgentCliReflector` | |
| `evolution.tree.agent_reflector:_read_result` → `evolution.tree.roles.reflector.agent_reflector:_read_result` | |
| `evolution.tree.chat_reflector:__all__` → `evolution.tree.roles.reflector.chat_reflector:__all__` | |
| `evolution.tree.chat_reflector:_ROLE` → `evolution.tree.roles.reflector.chat_reflector:_ROLE` | |
| `evolution.tree.chat_reflector:_GROUP_KEYS` → `evolution.tree.roles.reflector.chat_reflector:_GROUP_KEYS` | |
| `evolution.tree.chat_reflector:_group_schema` → `evolution.tree.roles.reflector.chat_reflector:_group_schema` | |
| `evolution.tree.chat_reflector:reflect_tools` → `evolution.tree.roles.reflector.chat_reflector:reflect_tools` | |
| `evolution.tree.chat_reflector:chat_first_messages` → `evolution.tree.roles.reflector.chat_reflector:chat_first_messages` | |
| `evolution.tree.chat_reflector:FINISH_REFLECTION_REPAIR_DESCRIPTION` → `evolution.tree.roles.reflector.chat_reflector:FINISH_REFLECTION_REPAIR_DESCRIPTION` | |
| `evolution.tree.chat_reflector:_TOOL_NAMES` → `evolution.tree.roles.reflector.chat_reflector:_TOOL_NAMES` | |
| `evolution.tree.chat_reflector:ChatReflector` → `evolution.tree.roles.reflector.chat_reflector:ChatReflector` | |
| `evolution.tree.chat_reflector:_ReflectSession` → `evolution.tree.roles.reflector.chat_reflector:_ReflectSession` | |
| `evolution.tree.chat_reflector:_terminal` → `evolution.tree.roles.reflector.chat_reflector:_terminal` | |
| `evolution.tree.chat_reflector:_provider_error_detail` → `evolution.tree.roles.reflector.chat_reflector:_provider_error_detail` | |
| `evolution.tree.subprocess_reflector:__all__` → `evolution.tree.roles.reflector.subprocess_reflector:__all__` | |
| `evolution.tree.subprocess_reflector:_STATUSES` → `evolution.tree.roles.reflector.subprocess_reflector:_STATUSES` | |
| `evolution.tree.subprocess_reflector:SubprocessReflector` → `evolution.tree.roles.reflector.subprocess_reflector:SubprocessReflector` | |
| `evolution.tree.subprocess_reflector:_request_document` → `evolution.tree.roles.reflector.subprocess_reflector:_request_document` | |
| `evolution.tree.subprocess_reflector:_classify` → `evolution.tree.roles.reflector.subprocess_reflector:_classify` | |
| `evolution.tree.subprocess_reflector:_result_line` → `evolution.tree.roles.reflector.subprocess_reflector:_result_line` | |
| `evolution.tree.subprocess_reflector:_SecretStream` → `evolution.tree.roles.reflector.subprocess_reflector:_SecretStream` | |
| `evolution.tree.subprocess_reflector:_redact_stderr` → `evolution.tree.roles.reflector.subprocess_reflector:_redact_stderr` | |
| `evolution.tree.subprocess_reflector:_probe_exchange` → `evolution.tree.roles.reflector.subprocess_reflector:_probe_exchange` | |
| `evolution.tree.reflect_aggregate:__all__` → `evolution.tree.roles.reflector.reflect_aggregate:__all__` | |
| `evolution.tree.reflect_aggregate:AGGREGATE_VERSION` → `evolution.tree.roles.reflector.reflect_aggregate:AGGREGATE_VERSION` | |
| `evolution.tree.reflect_aggregate:_PRESENT` → `evolution.tree.roles.reflector.reflect_aggregate:_PRESENT` | |
| `evolution.tree.reflect_aggregate:_PATH_TIERS` → `evolution.tree.roles.reflector.reflect_aggregate:_PATH_TIERS` | |
| `evolution.tree.reflect_aggregate:_CHANGE_TIERS` → `evolution.tree.roles.reflector.reflect_aggregate:_CHANGE_TIERS` | |
| `evolution.tree.reflect_aggregate:_GUARD_ORDER` → `evolution.tree.roles.reflector.reflect_aggregate:_GUARD_ORDER` | |
| `evolution.tree.reflect_aggregate:RoundRow` → `evolution.tree.roles.reflector.reflect_aggregate:RoundRow` | |
| `evolution.tree.reflect_aggregate:FactRow` → `evolution.tree.roles.reflector.reflect_aggregate:FactRow` | |
| `evolution.tree.reflect_aggregate:AggregateForms` → `evolution.tree.roles.reflector.reflect_aggregate:AggregateForms` | |
| `evolution.tree.reflect_aggregate:serialized_chars` → `evolution.tree.roles.reflector.reflect_aggregate:serialized_chars` | |
| `evolution.tree.reflect_aggregate:round_range` → `evolution.tree.roles.reflector.reflect_aggregate:round_range` | |
| `evolution.tree.reflect_aggregate:_nulls_first` → `evolution.tree.roles.reflector.reflect_aggregate:_nulls_first` | |
| `evolution.tree.reflect_aggregate:_newest` → `evolution.tree.roles.reflector.reflect_aggregate:_newest` | |
| `evolution.tree.reflect_aggregate:_ref` → `evolution.tree.roles.reflector.reflect_aggregate:_ref` | |
| `evolution.tree.reflect_aggregate:_tally` → `evolution.tree.roles.reflector.reflect_aggregate:_tally` | |
| `evolution.tree.reflect_aggregate:_change_pattern` → `evolution.tree.roles.reflector.reflect_aggregate:_change_pattern` | |
| `evolution.tree.reflect_aggregate:_path_pattern` → `evolution.tree.roles.reflector.reflect_aggregate:_path_pattern` | |
| `evolution.tree.reflect_aggregate:_witnesses` → `evolution.tree.roles.reflector.reflect_aggregate:_witnesses` | |
| `evolution.tree.reflect_aggregate:_pointer` → `evolution.tree.roles.reflector.reflect_aggregate:_pointer` | |
| `evolution.tree.reflect_aggregate:_path_entry` → `evolution.tree.roles.reflector.reflect_aggregate:_path_entry` | |
| `evolution.tree.reflect_aggregate:_guard` → `evolution.tree.roles.reflector.reflect_aggregate:_guard` | |
| `evolution.tree.reflect_aggregate:build_aggregate` → `evolution.tree.roles.reflector.reflect_aggregate:build_aggregate` | |
| `evolution.tree.reflect_aggregate:core_aggregate` → `evolution.tree.roles.reflector.reflect_aggregate:core_aggregate` | |
| `evolution.tree.reflect_aggregate:aggregate_forms` → `evolution.tree.roles.reflector.reflect_aggregate:aggregate_forms` | |
| `evolution.tree.reflect_brief:REFLECT_BRIEF_VERSION` → `evolution.tree.roles.reflector.reflect_brief:REFLECT_BRIEF_VERSION` | |
| `evolution.tree.reflect_brief:ReflectBrief` → `evolution.tree.roles.reflector.reflect_brief:ReflectBrief` | |
| `evolution.tree.reflect_brief:build_reflect_brief` → `evolution.tree.roles.reflector.reflect_brief:build_reflect_brief` | |
| `evolution.tree.reflect_brief:_repeat_payload` → `evolution.tree.roles.reflector.reflect_brief:_repeat_payload` | |
| `evolution.tree.reflect_brief:_others_payload` → `evolution.tree.roles.reflector.reflect_brief:_others_payload` | |
| `evolution.tree.reflect_brief:_portion_payload` → `evolution.tree.roles.reflector.reflect_brief:_portion_payload` | |
| `evolution.tree.reflect_brief:reflect_brief_payload` → `evolution.tree.roles.reflector.reflect_brief:reflect_brief_payload` | |
| `evolution.tree.reflect_brief:reflect_brief_digest` → `evolution.tree.roles.reflector.reflect_brief:reflect_brief_digest` | |
| `evolution.tree.reflect_brief:_render_reflect_summary` → `evolution.tree.roles.reflector.reflect_brief:_render_reflect_summary` | |
| `evolution.tree.reflect_brief:render_reflect_catalog` → `evolution.tree.roles.reflector.reflect_brief:render_reflect_catalog` | |
| `evolution.tree.reflect_brief:REFLECTOR_BUDGET_LINE` → `evolution.tree.roles.reflector.reflect_brief:REFLECTOR_BUDGET_LINE` | |
| `evolution.tree.reflect_brief:render_reflect_opening` → `evolution.tree.roles.reflector.reflect_brief:render_reflect_opening` | |
| `evolution.tree.reflect_brief:catalog_case_ids` → `evolution.tree.roles.reflector.reflect_brief:catalog_case_ids` | |
| `evolution.tree.reflect_brief:DIAGNOSTICS_INLINE_CHARS` → `evolution.tree.roles.reflector.reflect_brief:DIAGNOSTICS_INLINE_CHARS` | |
| `evolution.tree.reflect_brief:DIAGNOSTICS_OMITTED_KEYS` → `evolution.tree.roles.reflector.reflect_brief:DIAGNOSTICS_OMITTED_KEYS` | |
| `evolution.tree.reflect_brief:DIAGNOSTICS_KEY_CHARS` → `evolution.tree.roles.reflector.reflect_brief:DIAGNOSTICS_KEY_CHARS` | |
| `evolution.tree.reflect_brief:diagnostics_opening_form` → `evolution.tree.roles.reflector.reflect_brief:diagnostics_opening_form` | |
| `evolution.tree.reflect_brief:AdaptiveReflectBrief` → `evolution.tree.roles.reflector.reflect_brief:AdaptiveReflectBrief` | |
| `evolution.tree.reflect_brief:build_adaptive_reflect_brief` → `evolution.tree.roles.reflector.reflect_brief:build_adaptive_reflect_brief` | |
| `evolution.tree.reflect_brief:_freeze` → `evolution.tree.roles.reflector.reflect_brief:_freeze` | |
| `evolution.tree.reflect_brief:with_history` → `evolution.tree.roles.reflector.reflect_brief:with_history` | |
| `evolution.tree.reflect_budget:COMPONENTS` → `evolution.tree.roles.reflector.reflect_budget:COMPONENTS` | |
| `evolution.tree.reflect_budget:PROBE_URL_PLACEHOLDER` → `evolution.tree.roles.reflector.reflect_budget:PROBE_URL_PLACEHOLDER` | |
| `evolution.tree.reflect_budget:RequestBudget` → `evolution.tree.roles.reflector.reflect_budget:RequestBudget` | |
| `evolution.tree.reflect_budget:FirstRequest` → `evolution.tree.roles.reflector.reflect_budget:FirstRequest` | |
| `evolution.tree.reflect_budget:chat_request_size` → `evolution.tree.roles.reflector.reflect_budget:chat_request_size` | |
| `evolution.tree.reflect_budget:_payload_parts` → `evolution.tree.roles.reflector.reflect_budget:_payload_parts` | |
| `evolution.tree.reflect_budget:chat_first_request` → `evolution.tree.roles.reflector.reflect_budget:chat_first_request` | |
| `evolution.tree.reflect_budget:document_first_request` → `evolution.tree.roles.reflector.reflect_budget:document_first_request` | |
| `evolution.tree.reflect_budget:request_budget_of` → `evolution.tree.roles.reflector.reflect_budget:request_budget_of` | |
| `evolution.tree.reflect_budget:measure_first_request` → `evolution.tree.roles.reflector.reflect_budget:measure_first_request` | |
| `evolution.tree.reflect_budget:BoundHistory` → `evolution.tree.roles.reflector.reflect_budget:BoundHistory` | |
| `evolution.tree.reflect_budget:bound_reflection_history` → `evolution.tree.roles.reflector.reflect_budget:bound_reflection_history` | |
| `evolution.tree.reflect_budget:budget_record` → `evolution.tree.roles.reflector.reflect_budget:budget_record` | |
| `evolution.tree.reflect_budget:write_budget_record` → `evolution.tree.roles.reflector.reflect_budget:write_budget_record` | |
| `evolution.tree.reflect_change:__all__` → `evolution.tree.roles.reflector.reflect_change:__all__` | |
| `evolution.tree.reflect_change:_LINE_KINDS` → `evolution.tree.roles.reflector.reflect_change:_LINE_KINDS` | |
| `evolution.tree.reflect_change:Screen` → `evolution.tree.roles.reflector.reflect_change:Screen` | |
| `evolution.tree.reflect_change:SideChanged` → `evolution.tree.roles.reflector.reflect_change:SideChanged` | |
| `evolution.tree.reflect_change:Side` → `evolution.tree.roles.reflector.reflect_change:Side` | |
| `evolution.tree.reflect_change:PathChange` → `evolution.tree.roles.reflector.reflect_change:PathChange` | |
| `evolution.tree.reflect_change:_parent_fd` → `evolution.tree.roles.reflector.reflect_change:_parent_fd` | |
| `evolution.tree.reflect_change:_is_executable` → `evolution.tree.roles.reflector.reflect_change:_is_executable` | |
| `evolution.tree.reflect_change:_same_file` → `evolution.tree.roles.reflector.reflect_change:_same_file` | |
| `evolution.tree.reflect_change:read_side` → `evolution.tree.roles.reflector.reflect_change:read_side` | |
| `evolution.tree.reflect_change:_content` → `evolution.tree.roles.reflector.reflect_change:_content` | |
| `evolution.tree.reflect_change:change_record` → `evolution.tree.roles.reflector.reflect_change:change_record` | |
| `evolution.tree.reflect_change:fingerprint` → `evolution.tree.roles.reflector.reflect_change:fingerprint` | |
| `evolution.tree.reflect_change:literal_chars` → `evolution.tree.roles.reflector.reflect_change:literal_chars` | |
| `evolution.tree.reflect_change:cut_to_literal` → `evolution.tree.roles.reflector.reflect_change:cut_to_literal` | |
| `evolution.tree.reflect_change:excerpt_screen` → `evolution.tree.roles.reflector.reflect_change:excerpt_screen` | |
| `evolution.tree.reflect_change:describe` → `evolution.tree.roles.reflector.reflect_change:describe` | |
| `evolution.tree.reflect_change:path_change` → `evolution.tree.roles.reflector.reflect_change:path_change` | |
| `evolution.tree.reflect_change:path_display` → `evolution.tree.roles.reflector.reflect_change:path_display` | |
| `evolution.tree.reflect_evidence:GROUPING_VERSION` → `evolution.tree.roles.reflector.reflect_evidence:GROUPING_VERSION` | |
| `evolution.tree.reflect_evidence:SCALES` → `evolution.tree.roles.reflector.reflect_evidence:SCALES` | |
| `evolution.tree.reflect_evidence:RepeatEvidence` → `evolution.tree.roles.reflector.reflect_evidence:RepeatEvidence` | |
| `evolution.tree.reflect_evidence:REDACTED` → `evolution.tree.execution.input_secrets:REDACTED` | |
| `evolution.tree.reflect_evidence:input_secret_value` → `evolution.tree.roles.input_secrets:input_secret_value` | |
| `evolution.tree.reflect_evidence:criteria_carry_run_secret` → `evolution.tree.roles.input_secrets:criteria_carry_run_secret` | |
| `evolution.tree.reflect_evidence:redact_run_secrets` → `evolution.tree.execution.input_secrets:redact_run_secrets` | |
| `evolution.tree.reflect_evidence:_unavailable_repeat` → `evolution.tree.roles.reflector.reflect_evidence:_unavailable_repeat` | |
| `evolution.tree.reflect_evidence:_Feedback` → `evolution.tree.roles.reflector.reflect_evidence:_Feedback` | |
| `evolution.tree.reflect_evidence:_reflect_feedback` → `evolution.tree.roles.reflector.reflect_evidence:_reflect_feedback` | |
| `evolution.tree.reflect_evidence:_repeat_evidence` → `evolution.tree.roles.reflector.reflect_evidence:_repeat_evidence` | |
| `evolution.tree.reflect_evidence:project_repeats` → `evolution.tree.roles.reflector.reflect_evidence:project_repeats` | |
| `evolution.tree.reflect_evidence:scale_buckets` → `evolution.tree.roles.reflector.reflect_evidence:scale_buckets` | |
| `evolution.tree.reflect_evidence:Cohort` → `evolution.tree.roles.reflector.reflect_evidence:Cohort` | |
| `evolution.tree.reflect_evidence:cohorts_of` → `evolution.tree.roles.reflector.reflect_evidence:cohorts_of` | |
| `evolution.tree.reflect_evidence:Cluster` → `evolution.tree.roles.reflector.reflect_evidence:Cluster` | |
| `evolution.tree.reflect_evidence:_ClusterKey` → `evolution.tree.roles.reflector.reflect_evidence:_ClusterKey` | |
| `evolution.tree.reflect_evidence:clusters_of` → `evolution.tree.roles.reflector.reflect_evidence:clusters_of` | |
| `evolution.tree.reflect_evidence:stalled_fingerprints` → `evolution.tree.roles.reflector.reflect_evidence:stalled_fingerprints` | |
| `evolution.tree.reflect_evidence:FocusSelection` → `evolution.tree.roles.reflector.reflect_evidence:FocusSelection` | |
| `evolution.tree.reflect_evidence:select_focus` → `evolution.tree.roles.reflector.reflect_evidence:select_focus` | |
| `evolution.tree.reflect_evidence:PortionCohort` → `evolution.tree.roles.reflector.reflect_evidence:PortionCohort` | |
| `evolution.tree.reflect_evidence:_contrast_of` → `evolution.tree.roles.reflector.reflect_evidence:_contrast_of` | |
| `evolution.tree.reflect_evidence:portion_of` → `evolution.tree.roles.reflector.reflect_evidence:portion_of` | |
| `evolution.tree.reflect_evidence:policy_digest` → `evolution.tree.roles.reflector.reflect_evidence:policy_digest` | |
| `evolution.tree.reflect_history:HISTORY_VERSION` → `evolution.tree.roles.reflector.reflect_history:HISTORY_VERSION` | |
| `evolution.tree.reflect_history:READER_LIMITS` → `evolution.tree.roles.reflector.reflect_history:READER_LIMITS` | |
| `evolution.tree.reflect_history:_EDITOR_TERMINALS` → `evolution.tree.roles.reflector.reflect_history:_EDITOR_TERMINALS` | |
| `evolution.tree.reflect_history:_block` → `evolution.tree.roles.reflector.reflect_history:_block` | |
| `evolution.tree.reflect_history:attempt_scores` → `evolution.tree.roles.reflector.reflect_history:attempt_scores` | |
| `evolution.tree.reflect_history:AttemptFacts` → `evolution.tree.roles.reflector.reflect_history:AttemptFacts` | |
| `evolution.tree.reflect_history:attempt_facts` → `evolution.tree.roles.reflector.reflect_history:attempt_facts` | |
| `evolution.tree.reflect_history:_cut` → `evolution.tree.roles.reflector.reflect_history:_cut` | |
| `evolution.tree.reflect_history:_hypothesis` → `evolution.tree.roles.reflector.reflect_history:_hypothesis` | |
| `evolution.tree.reflect_history:_attempt_entry` → `evolution.tree.roles.reflector.reflect_history:_attempt_entry` | |
| `evolution.tree.reflect_history:ClosedRound` → `evolution.tree.roles.reflector.reflect_history:ClosedRound` | |
| `evolution.tree.reflect_history:closed_round` → `evolution.tree.roles.reflector.reflect_history:closed_round` | |
| `evolution.tree.reflect_history:HistorySource` → `evolution.tree.roles.reflector.reflect_history:HistorySource` | |
| `evolution.tree.reflect_history:MemoKey` → `evolution.tree.roles.reflector.reflect_history:MemoKey` | |
| `evolution.tree.reflect_history:build_history_source` → `evolution.tree.roles.reflector.reflect_history:build_history_source` | |
| `evolution.tree.reflect_history:Layout` → `evolution.tree.roles.reflector.reflect_history:Layout` | |
| `evolution.tree.reflect_history:initial_layout` → `evolution.tree.roles.reflector.reflect_history:initial_layout` | |
| `evolution.tree.reflect_history:elision_steps` → `evolution.tree.roles.reflector.reflect_history:elision_steps` | |
| `evolution.tree.reflect_history:render_history` → `evolution.tree.roles.reflector.reflect_history:render_history` | |
| `evolution.tree.reflect_history:bound_history` → `evolution.tree.roles.reflector.reflect_history:bound_history` | |
| `evolution.tree.reflect_history:HISTORY_COMPACT_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:HISTORY_COMPACT_SENTENCE` | |
| `evolution.tree.reflect_history:HISTORY_READ_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:HISTORY_READ_SENTENCE` | |
| `evolution.tree.reflect_history:AGGREGATE_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:AGGREGATE_SENTENCE` | |
| `evolution.tree.reflect_history:FINGERPRINT_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:FINGERPRINT_SENTENCE` | |
| `evolution.tree.reflect_history:AGGREGATE_READ_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:AGGREGATE_READ_SENTENCE` | |
| `evolution.tree.reflect_history:WINDOW_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:WINDOW_SENTENCE` | |
| `evolution.tree.reflect_history:BUDGET_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:BUDGET_SENTENCE` | |
| `evolution.tree.reflect_history:REDUCED_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:REDUCED_SENTENCE` | |
| `evolution.tree.reflect_history:CORE_SENTENCE` → `evolution.tree.roles.reflector.reflect_history:CORE_SENTENCE` | |
| `evolution.tree.reflect_history:history_sentences` → `evolution.tree.roles.reflector.reflect_history:history_sentences` | |
| `evolution.tree.reflect_history:compact_budget_reason_sentences` → `evolution.tree.roles.reflector.reflect_history:compact_budget_reason_sentences` | |
| `evolution.tree.reflect_history:ATTEMPT_CHANGES_VERSION` → `evolution.tree.roles.reflector.reflect_history:ATTEMPT_CHANGES_VERSION` | |
| `evolution.tree.reflect_history:attempt_changes_record` → `evolution.tree.roles.reflector.reflect_history:attempt_changes_record` | |
| `evolution.tree.reflect_history:_arguments` → `evolution.tree.roles.reflector.reflect_history:_arguments` | |
| `evolution.tree.reflect_history:HistoryReader` → `evolution.tree.roles.reflector.reflect_history:HistoryReader` | |
| `evolution.tree.reflection_inputs:GROUPING_PAGE_SIZE` → `evolution.tree.roles.reflection_reader:GROUPING_PAGE_SIZE` | |
| `evolution.tree.reflection_inputs:CONTENT_CHARS` → `evolution.tree.roles.reflection_reader:CONTENT_CHARS` | |
| `evolution.tree.reflection_inputs:EVIDENCE_PAGE_SIZE` → `evolution.tree.roles.reflector.reflection_inputs:EVIDENCE_PAGE_SIZE` | |
| `evolution.tree.reflection_inputs:SUMMARY_CASE_ID_BYTES` → `evolution.tree.roles.reflector.reflection_inputs:SUMMARY_CASE_ID_BYTES` | |
| `evolution.tree.reflection_inputs:SUMMARY_VARIANTS` → `evolution.tree.roles.reflector.reflection_inputs:SUMMARY_VARIANTS` | |
| `evolution.tree.reflection_inputs:__all__` → `evolution.tree.roles.reflector.reflection_inputs:__all__` | |
| `evolution.tree.reflection_inputs:_canonical` → `evolution.tree.roles.reflector.reflection_inputs:_canonical` | |
| `evolution.tree.reflection_inputs:_digest` → `evolution.tree.roles.reflector.reflection_inputs:_digest` | |
| `evolution.tree.reflection_inputs:_page` → `evolution.tree.roles.reflector.reflection_inputs:_page` | |
| `evolution.tree.reflection_inputs:_offset` → `evolution.tree.roles.reflector.reflection_inputs:_offset` | |
| `evolution.tree.reflection_inputs:_bounded_entries` → `evolution.tree.roles.reflector.reflection_inputs:_bounded_entries` | |
| `evolution.tree.reflection_inputs:_Header` → `evolution.tree.roles.reflector.reflection_inputs:_Header` | |
| `evolution.tree.reflection_inputs:_Sources` → `evolution.tree.roles.reflector.reflection_inputs:_Sources` | |
| `evolution.tree.reflection_inputs:LazyObservationMapping` → `evolution.tree.roles.reflector.reflection_inputs:LazyObservationMapping` | |
| `evolution.tree.reflection_inputs:FrozenReflectionInputs` → `evolution.tree.roles.reflector.reflection_inputs:FrozenReflectionInputs` | |
| `evolution.tree.agent_editor:CONTROL_PREFIX` → `evolution.tree.roles.editor.agent_editor:CONTROL_PREFIX` | |
| `evolution.tree.agent_editor:REQUEST_VERSION` → `evolution.tree.roles.editor.agent_editor:REQUEST_VERSION` | |
| `evolution.tree.agent_editor:_REQUEST_FILE` → `evolution.tree.roles.editor.agent_editor:_REQUEST_FILE` | |
| `evolution.tree.agent_editor:_VALIDATION_FILE` → `evolution.tree.roles.editor.agent_editor:_VALIDATION_FILE` | |
| `evolution.tree.agent_editor:_EVIDENCE_DIR` → `evolution.tree.roles.editor.agent_editor:_EVIDENCE_DIR` | |
| `evolution.tree.agent_editor:_EVIDENCE_INDEX` → `evolution.tree.roles.editor.agent_editor:_EVIDENCE_INDEX` | |
| `evolution.tree.agent_editor:Outcome` → `evolution.tree.roles.editor.agent_editor:Outcome` | |
| `evolution.tree.agent_editor:AGENT_VALIDATION_LINE` → `evolution.tree.roles.editor.agent_editor:AGENT_VALIDATION_LINE` | |
| `evolution.tree.agent_editor:_NO_CHANGE` → `evolution.tree.roles.editor.agent_editor:_NO_CHANGE` | |
| `evolution.tree.agent_editor:_PUBLISHED` → `evolution.tree.roles.editor.agent_editor:_PUBLISHED` | |
| `evolution.tree.agent_editor:_Refused` → `evolution.tree.roles.editor.agent_editor:_Refused` | |
| `evolution.tree.agent_editor:AgentCliEditor` → `evolution.tree.roles.editor.agent_editor:AgentCliEditor` | |
| `evolution.tree.agent_editor:_refusal` → `evolution.tree.roles.editor.agent_editor:_refusal` | |
| `evolution.tree.agent_editor:_changed_paths` → `evolution.tree.roles.editor.agent_editor:_changed_paths` | |
| `evolution.tree.agent_editor:_write_json` → `evolution.tree.roles.editor.agent_editor:_write_json` | |
| `evolution.tree.chat_editor:__all__` → `evolution.tree.roles.editor.chat_editor:__all__` | |
| `evolution.tree.chat_editor:MAX_PROTOCOL_REPAIRS` → `evolution.tree.roles.editor.chat_editor:MAX_PROTOCOL_REPAIRS` | |
| `evolution.tree.chat_editor:MAX_CONSECUTIVE_TOOL_FAILURES` → `evolution.tree.roles.editor.chat_editor:MAX_CONSECUTIVE_TOOL_FAILURES` | |
| `evolution.tree.chat_editor:PROVIDER_ATTEMPTS` → `evolution.tree.roles.editor.chat_editor:PROVIDER_ATTEMPTS` | |
| `evolution.tree.chat_editor:PROTOCOL_PREVIEW_CHARS` → `evolution.tree.roles.editor.chat_editor:PROTOCOL_PREVIEW_CHARS` | |
| `evolution.tree.chat_editor:MAX_READ_FILE_CHARS` → `evolution.tree.roles.editor.chat_editor:MAX_READ_FILE_CHARS` | |
| `evolution.tree.chat_editor:FILE_SUMMARY_MAX_CHARS` → `evolution.tree.roles.editor.chat_editor:FILE_SUMMARY_MAX_CHARS` | |
| `evolution.tree.chat_editor:EVIDENCE_PAGE_RECORDS` → `evolution.tree.roles.editor.chat_editor:EVIDENCE_PAGE_RECORDS` | |
| `evolution.tree.chat_editor:EVIDENCE_PAGE_BYTES` → `evolution.tree.roles.editor.chat_editor:EVIDENCE_PAGE_BYTES` | |
| `evolution.tree.chat_editor:EVIDENCE_CHUNK_CHARS` → `evolution.tree.roles.editor.chat_editor:EVIDENCE_CHUNK_CHARS` | |
| `evolution.tree.chat_editor:CONTEXT_TAIL_MESSAGES` → `evolution.tree.roles.editor.chat_editor:CONTEXT_TAIL_MESSAGES` | |
| `evolution.tree.chat_editor:CLIENT_CLOSE_GRACE_SECONDS` → `evolution.tree.roles.editor.chat_editor:CLIENT_CLOSE_GRACE_SECONDS` | |
| `evolution.tree.chat_editor:Outcome` → `evolution.tree.roles.editor.chat_editor:Outcome` | |
| `evolution.tree.chat_editor:ToolError` → `evolution.tree.roles.editor.chat_editor:ToolError` | |
| `evolution.tree.chat_editor:PlanCorrection` → `evolution.tree.roles.editor.chat_editor:PlanCorrection` | |
| `evolution.tree.chat_editor:EditFailure` → `evolution.tree.roles.editor.chat_editor:EditFailure` | |
| `evolution.tree.chat_editor:ProviderIdentityError` → `evolution.tree.roles.editor.chat_editor:ProviderIdentityError` | |
| `evolution.tree.chat_editor:ChatCredentials` → `evolution.tree.roles.editor.chat_editor:ChatCredentials` | |
| `evolution.tree.chat_editor:CREDENTIAL_HEADERS` → `evolution.tree.roles.editor.chat_editor:CREDENTIAL_HEADERS` | |
| `evolution.tree.chat_editor:PROTOCOL_HEADERS` → `evolution.tree.roles.editor.chat_editor:PROTOCOL_HEADERS` | |
| `evolution.tree.chat_editor:UNAUTHENTICATED_API_KEY` → `evolution.tree.roles.editor.chat_editor:UNAUTHENTICATED_API_KEY` | |
| `evolution.tree.chat_editor:environment_header_names` → `evolution.tree.roles.editor.chat_editor:environment_header_names` | |
| `evolution.tree.chat_editor:credential_free_transport` → `evolution.tree.roles.editor.chat_editor:credential_free_transport` | |
| `evolution.tree.chat_editor:chat_http_client` → `evolution.tree.roles.editor.chat_editor:chat_http_client` | |
| `evolution.tree.chat_editor:sdk_credentials` → `evolution.tree.roles.editor.chat_editor:sdk_credentials` | |
| `evolution.tree.chat_editor:request_options` → `evolution.tree.roles.editor.chat_editor:request_options` | |
| `evolution.tree.chat_editor:_TOOL_NAMES` → `evolution.tree.roles.editor.chat_editor:_TOOL_NAMES` | |
| `evolution.tree.chat_editor:_MUTATING_TOOLS` → `evolution.tree.roles.editor.chat_editor:_MUTATING_TOOLS` | |
| `evolution.tree.chat_editor:_tool` → `evolution.tree.roles.editor.chat_editor:_tool` | |
| `evolution.tree.chat_editor:TOOLS` → `evolution.tree.roles.editor.chat_editor:TOOLS` | |
| `evolution.tree.chat_editor:_TRACE_TOOL` → `evolution.tree.roles.editor.chat_editor:_TRACE_TOOL` | |
| `evolution.tree.chat_editor:_TRACE_POSITION` → `evolution.tree.roles.editor.chat_editor:_TRACE_POSITION` | |
| `evolution.tree.chat_editor:TOOLS_V16` → `evolution.tree.roles.editor.chat_editor:TOOLS_V16` | |
| `evolution.tree.chat_editor:AUTO_PURPOSE` → `evolution.tree.roles.editor.chat_editor:AUTO_PURPOSE` | |
| `evolution.tree.chat_editor:PlanChange` → `evolution.tree.roles.editor.chat_editor:PlanChange` | |
| `evolution.tree.chat_editor:Plan` → `evolution.tree.roles.editor.chat_editor:Plan` | |
| `evolution.tree.chat_editor:_covers` → `evolution.tree.roles.editor.chat_editor:_covers` | |
| `evolution.tree.chat_editor:_BUDGET_PATH_LIMIT` → `evolution.tree.roles.editor.chat_editor:_BUDGET_PATH_LIMIT` | |
| `evolution.tree.chat_editor:_files` → `evolution.tree.roles.editor.chat_editor:_files` | |
| `evolution.tree.chat_editor:_path_list` → `evolution.tree.roles.editor.chat_editor:_path_list` | |
| `evolution.tree.chat_editor:_final_planned_actions` → `evolution.tree.roles.editor.chat_editor:_final_planned_actions` | |
| `evolution.tree.chat_editor:_changed_actions` → `evolution.tree.roles.editor.chat_editor:_changed_actions` | |
| `evolution.tree.chat_editor:_tool_calls` → `evolution.tree.roles.editor.chat_editor:_tool_calls` | |
| `evolution.tree.chat_editor:_call_name` → `evolution.tree.roles.editor.chat_editor:_call_name` | |
| `evolution.tree.chat_editor:_raw_call_arguments` → `evolution.tree.roles.editor.chat_editor:_raw_call_arguments` | |
| `evolution.tree.chat_editor:_call_arguments` → `evolution.tree.roles.editor.chat_editor:_call_arguments` | |
| `evolution.tree.chat_editor:_call_id` → `evolution.tree.roles.editor.chat_editor:_call_id` | |
| `evolution.tree.chat_editor:_encoded_call_arguments` → `evolution.tree.roles.editor.chat_editor:_encoded_call_arguments` | |
| `evolution.tree.chat_editor:_response_message` → `evolution.tree.roles.editor.chat_editor:_response_message` | |
| `evolution.tree.chat_editor:_tool_result_messages` → `evolution.tree.roles.editor.chat_editor:_tool_result_messages` | |
| `evolution.tree.chat_editor:_text_summary` → `evolution.tree.roles.editor.chat_editor:_text_summary` | |
| `evolution.tree.chat_editor:_protocol_correction` → `evolution.tree.roles.editor.chat_editor:_protocol_correction` | |
| `evolution.tree.chat_editor:_compact_message` → `evolution.tree.roles.editor.chat_editor:_compact_message` | |
| `evolution.tree.chat_editor:_compact_messages` → `evolution.tree.roles.editor.chat_editor:_compact_messages` | |
| `evolution.tree.chat_editor:_provider_name` → `evolution.tree.roles.editor.chat_editor:_provider_name` | |
| `evolution.tree.chat_editor:_returned_model` → `evolution.tree.roles.editor.chat_editor:_returned_model` | |
| `evolution.tree.chat_editor:_returned_provider` → `evolution.tree.roles.editor.chat_editor:_returned_provider` | |
| `evolution.tree.chat_editor:_requested_provider` → `evolution.tree.roles.editor.chat_editor:_requested_provider` | |
| `evolution.tree.chat_editor:_provider_matches` → `evolution.tree.roles.editor.chat_editor:_provider_matches` | |
| `evolution.tree.chat_editor:_read_text` → `evolution.tree.roles.editor.chat_editor:_read_text` | |
| `evolution.tree.chat_editor:_read_file_range` → `evolution.tree.roles.editor.chat_editor:_read_file_range` | |
| `evolution.tree.chat_editor:_markdown_sections` → `evolution.tree.roles.editor.chat_editor:_markdown_sections` | |
| `evolution.tree.chat_editor:_yaml_sections` → `evolution.tree.roles.editor.chat_editor:_yaml_sections` | |
| `evolution.tree.chat_editor:_inspect_file` → `evolution.tree.roles.editor.chat_editor:_inspect_file` | |
| `evolution.tree.chat_editor:_file_summary` → `evolution.tree.roles.editor.chat_editor:_file_summary` | |
| `evolution.tree.chat_editor:readable_path` → `evolution.tree.roles.editor.chat_editor:readable_path` | |
| `evolution.tree.chat_editor:readable_files` → `evolution.tree.roles.editor.chat_editor:readable_files` | |
| `evolution.tree.chat_editor:limited` → `evolution.tree.roles.editor.chat_editor:limited` | |
| `evolution.tree.chat_editor:read_file_tool` → `evolution.tree.roles.editor.chat_editor:read_file_tool` | |
| `evolution.tree.chat_editor:_evidence_page` → `evolution.tree.roles.editor.chat_editor:_evidence_page` | |
| `evolution.tree.chat_editor:_evidence_chunk` → `evolution.tree.roles.editor.chat_editor:_evidence_chunk` | |
| `evolution.tree.chat_editor:_parse_patch` → `evolution.tree.roles.editor.chat_editor:_parse_patch` | |
| `evolution.tree.chat_editor:_hunk_position` → `evolution.tree.roles.editor.chat_editor:_hunk_position` | |
| `evolution.tree.chat_editor:_apply_hunks` → `evolution.tree.roles.editor.chat_editor:_apply_hunks` | |
| `evolution.tree.chat_editor:ChatEditor` → `evolution.tree.roles.editor.chat_editor:ChatEditor` | |
| `evolution.tree.chat_editor:_refusal` → `evolution.tree.roles.editor.chat_editor:_refusal` | |
| `evolution.tree.chat_editor:_new_client` → `evolution.tree.roles.editor.chat_editor:_new_client` | |
| `evolution.tree.chat_editor:_close_client` → `evolution.tree.roles.editor.chat_editor:_close_client` | |
| `evolution.tree.chat_editor:_consume_late_task` → `evolution.tree.roles.editor.chat_editor:_consume_late_task` | |
| `evolution.tree.chat_editor:record_request` → `evolution.tree.roles.editor.chat_editor:record_request` | |
| `evolution.tree.chat_editor:_protocol_options` → `evolution.tree.roles.editor.chat_editor:_protocol_options` | |
| `evolution.tree.chat_editor:_verify_identity` → `evolution.tree.roles.editor.chat_editor:_verify_identity` | |
| `evolution.tree.chat_editor:_provider_attempt_record` → `evolution.tree.roles.editor.chat_editor:_provider_attempt_record` | |
| `evolution.tree.chat_editor:provider_response` → `evolution.tree.roles.editor.chat_editor:provider_response` | |
| `evolution.tree.chat_editor:provider_request` → `evolution.tree.roles.editor.chat_editor:provider_request` | |
| `evolution.tree.chat_editor:_request_failure_detail` → `evolution.tree.roles.editor.chat_editor:_request_failure_detail` | |
| `evolution.tree.chat_editor:_dialect_rejection` → `evolution.tree.roles.editor.chat_editor:_dialect_rejection` | |
| `evolution.tree.chat_editor:negotiate_style` → `evolution.tree.roles.editor.chat_editor:negotiate_style` | |
| `evolution.tree.chat_editor:_negotiate_style` → `evolution.tree.roles.editor.chat_editor:_negotiate_style` | |
| `evolution.tree.chat_editor:_Session` → `evolution.tree.roles.editor.chat_editor:_Session` | |
| `evolution.tree.chat_editor:_parents_to_root` → `evolution.tree.roles.editor.chat_editor:_parents_to_root` | |
| `evolution.tree.chat_editor:_expect` → `evolution.tree.roles.editor.chat_editor:_expect` | |
| `evolution.tree.chat_editor:_offset` → `evolution.tree.roles.editor.chat_editor:_offset` | |
| `evolution.tree.chat_editor:_request_audit` → `evolution.tree.roles.editor.chat_editor:_request_audit` | |
| `evolution.tree.chat_editor:_message_key` → `evolution.tree.roles.editor.chat_editor:_message_key` | |
| `evolution.tree.chat_editor:_shared_prefix` → `evolution.tree.roles.editor.chat_editor:_shared_prefix` | |
| `evolution.tree.chat_editor:_response_audit` → `evolution.tree.roles.editor.chat_editor:_response_audit` | |
| `evolution.tree.chat_editor:_preflight_failure` → `evolution.tree.roles.editor.chat_editor:_preflight_failure` | |
| `evolution.tree.subprocess_editor:REQUEST_VERSION` → `evolution.tree.roles.editor.subprocess_editor:REQUEST_VERSION` | |
| `evolution.tree.subprocess_editor:MAX_STDOUT_BYTES` → `evolution.tree.roles.editor.subprocess_editor:MAX_STDOUT_BYTES` | |
| `evolution.tree.subprocess_editor:MAX_NOTE_BYTES` → `evolution.tree.roles.editor.subprocess_editor:MAX_NOTE_BYTES` | |
| `evolution.tree.subprocess_editor:KILL_GRACE_SECONDS` → `evolution.tree.roles.editor.subprocess_editor:KILL_GRACE_SECONDS` | |
| `evolution.tree.subprocess_editor:_DRAIN_CHUNK_BYTES` → `evolution.tree.roles.editor.subprocess_editor:_DRAIN_CHUNK_BYTES` | |
| `evolution.tree.subprocess_editor:_STATUSES` → `evolution.tree.roles.editor.subprocess_editor:_STATUSES` | |
| `evolution.tree.subprocess_editor:PROJECTED_BUDGETS` → `evolution.tree.roles.editor.subprocess_editor:PROJECTED_BUDGETS` | |
| `evolution.tree.subprocess_editor:__all__` → `evolution.tree.roles.editor.subprocess_editor:__all__` | |
| `evolution.tree.subprocess_editor:SubprocessEditor` → `evolution.tree.roles.editor.subprocess_editor:SubprocessEditor` | |
| `evolution.tree.subprocess_editor:_exchange` → `evolution.tree.roles.editor.subprocess_editor:_exchange` | |
| `evolution.tree.subprocess_editor:_write_request` → `evolution.tree.roles.editor.subprocess_editor:_write_request` | |
| `evolution.tree.subprocess_editor:_read_stdout` → `evolution.tree.roles.editor.subprocess_editor:_read_stdout` | |
| `evolution.tree.subprocess_editor:_terminate_group` → `evolution.tree.roles.editor.subprocess_editor:_terminate_group` | |
| `evolution.tree.subprocess_editor:_terminate_shielded` → `evolution.tree.roles.editor.subprocess_editor:_terminate_shielded` | |
| `evolution.tree.subprocess_editor:_signal_group` → `evolution.tree.roles.editor.subprocess_editor:_signal_group` | |
| `evolution.tree.subprocess_editor:_request_document` → `evolution.tree.roles.editor.subprocess_editor:_request_document` | |
| `evolution.tree.subprocess_editor:_open_private_log` → `evolution.tree.roles.editor.subprocess_editor:_open_private_log` | |
| `evolution.tree.subprocess_editor:_classify` → `evolution.tree.roles.editor.subprocess_editor:_classify` | |
| `evolution.tree.subprocess_editor:_result_line` → `evolution.tree.roles.editor.subprocess_editor:_result_line` | |
| `evolution.tree.editor_brief:SUMMARY_ID` → `evolution.tree.roles.editor.editor_brief:SUMMARY_ID` | |
| `evolution.tree.editor_brief:FILE_BUDGET_LINE` → `evolution.tree.roles.editor.editor_brief:FILE_BUDGET_LINE` | |
| `evolution.tree.editor_brief:SUBSET_LINE` → `evolution.tree.roles.editor.editor_brief:SUBSET_LINE` | |
| `evolution.tree.editor_brief:BUDGET_REASON_SENTENCES` → `evolution.tree.roles.editor.editor_brief:BUDGET_REASON_SENTENCES` | |
| `evolution.tree.editor_brief:budget_reason_sentences` → `evolution.tree.roles.editor.editor_brief:budget_reason_sentences` | |
| `evolution.tree.editor_brief:EvidenceRecord` → `evolution.tree.roles.editor.editor_brief:EvidenceRecord` | |
| `evolution.tree.editor_brief:_SAFE_CASE_ID_CHAR` → `evolution.tree.roles.editor.editor_brief:_SAFE_CASE_ID_CHAR` | |
| `evolution.tree.editor_brief:_CASE_ID_PREFIX` → `evolution.tree.roles.editor.editor_brief:_CASE_ID_PREFIX` | |
| `evolution.tree.editor_brief:_EVIDENCE_ID_CHARS` → `evolution.tree.roles.editor.editor_brief:_EVIDENCE_ID_CHARS` | |
| `evolution.tree.editor_brief:_CASE_ID_ENCODED_CHARS` → `evolution.tree.roles.editor.editor_brief:_CASE_ID_ENCODED_CHARS` | |
| `evolution.tree.editor_brief:_CASE_ID_DIGEST_CHARS` → `evolution.tree.roles.editor.editor_brief:_CASE_ID_DIGEST_CHARS` | |
| `evolution.tree.editor_brief:_CASE_ID_STEM_CHARS` → `evolution.tree.roles.editor.editor_brief:_CASE_ID_STEM_CHARS` | |
| `evolution.tree.editor_brief:_case_evidence_id` → `evolution.tree.roles.editor.editor_brief:_case_evidence_id` | |
| `evolution.tree.editor_brief:_encode_case_id` → `evolution.tree.roles.editor.editor_brief:_encode_case_id` | |
| `evolution.tree.editor_brief:_OBSERVATION_ID_PREFIX` → `evolution.tree.roles.editor.editor_brief:_OBSERVATION_ID_PREFIX` | |
| `evolution.tree.editor_brief:_observation_evidence_id` → `evolution.tree.roles.editor.editor_brief:_observation_evidence_id` | |
| `evolution.tree.editor_brief:DIAGNOSIS_ID` → `evolution.tree.roles.editor.editor_brief:DIAGNOSIS_ID` | |
| `evolution.tree.editor_brief:render_catalog` → `evolution.tree.roles.editor.editor_brief:render_catalog` | |
| `evolution.tree.editor_brief:_render_summary` → `evolution.tree.roles.editor.editor_brief:_render_summary` | |
| `evolution.tree.editor_brief:_render_diagnosis` → `evolution.tree.roles.editor.editor_brief:_render_diagnosis` | |
| `evolution.tree.editor_brief:render_opening` → `evolution.tree.roles.editor.editor_brief:render_opening` | |
| `evolution.tree.editor_brief:_render_cited` → `evolution.tree.roles.editor.editor_brief:_render_cited` | |
| `evolution.tree.editor_evidence:RowSource` → `evolution.tree.roles.editor.editor_evidence:RowSource` | |
| `evolution.tree.editor_evidence:PublishedRefs` → `evolution.tree.roles.editor.editor_evidence:PublishedRefs` | |
| `evolution.tree.editor_evidence:project_attempt_feedback` → `evolution.tree.roles.editor.editor_evidence:project_attempt_feedback` | |
| `evolution.tree.editor_evidence:_files` → `evolution.tree.roles.editor.editor_evidence:_files` | |
| `evolution.tree.editor_evidence:_feedback_lines` → `evolution.tree.roles.editor.editor_evidence:_feedback_lines` | |
| `evolution.tree.editor_evidence:_feedback_scores` → `evolution.tree.roles.editor.editor_evidence:_feedback_scores` | |
| `evolution.tree.editor_evidence:project_history` → `evolution.tree.roles.editor.editor_evidence:project_history` | |
| `evolution.tree.editor_evidence:_diagnosis_payload` → `evolution.tree.roles.editor.editor_evidence:_diagnosis_payload` | |
| `evolution.tree.editor_evidence:_history_attempt` → `evolution.tree.roles.editor.editor_evidence:_history_attempt` | |
| `evolution.tree.editor_evidence:_verify_published_copies` → `evolution.tree.roles.editor.editor_evidence:_verify_published_copies` | |
| `evolution.tree.editor_evidence:_changes` → `evolution.tree.roles.editor.editor_evidence:_changes` | |
| `evolution.tree.editor_evidence:_changes_with_scans` → `evolution.tree.roles.editor.editor_evidence:_changes_with_scans` | |
| `evolution.tree.editor_evidence:_edit_excerpt` → `evolution.tree.roles.editor.editor_evidence:_edit_excerpt` | |
| `evolution.tree.editor_evidence:_lines` → `evolution.tree.roles.editor.editor_evidence:_lines` | |
| `evolution.tree.editor_evidence:_result` → `evolution.tree.roles.editor.editor_evidence:_result` | |
| `evolution.tree.editor_evidence:result_cases` → `evolution.tree.roles.editor.editor_evidence:result_cases` | |
| `evolution.tree.editor_evidence:_IDENTIFIER` → `evolution.tree.roles.editor.editor_evidence:_IDENTIFIER` | |
| `evolution.tree.editor_evidence:_FEEDBACK_STRING` → `evolution.tree.roles.editor.editor_evidence:_FEEDBACK_STRING` | |
| `evolution.tree.editor_evidence:_INPUT_KEYS` → `evolution.tree.roles.editor.editor_evidence:_INPUT_KEYS` | |
| `evolution.tree.editor_evidence:_REQUEST_KEYS` → `evolution.tree.roles.editor.editor_evidence:_REQUEST_KEYS` | |
| `evolution.tree.editor_evidence:_PATH_MAX` → `evolution.tree.roles.editor.editor_evidence:_PATH_MAX` | |
| `evolution.tree.editor_evidence:_NAME_MAX` → `evolution.tree.roles.editor.editor_evidence:_NAME_MAX` | |
| `evolution.tree.editor_evidence:_identifier` → `evolution.tree.roles.editor.editor_evidence:_identifier` | |
| `evolution.tree.editor_evidence:_artifact_path` → `evolution.tree.roles.editor.editor_evidence:_artifact_path` | |
| `evolution.tree.editor_evidence:shown_event` → `evolution.tree.roles.editor.editor_evidence:shown_event` | |
| `evolution.tree.editor_evidence:_scalar_ok` → `evolution.tree.roles.editor.editor_evidence:_scalar_ok` | |
| `evolution.tree.editor_evidence:feedback_of` → `evolution.tree.roles.editor.editor_evidence:feedback_of` | |
| `evolution.tree.editor_evidence:_TraceView` → `evolution.tree.roles.editor.editor_evidence:_TraceView` | |
| `evolution.tree.editor_evidence:CarriedTraces` → `evolution.tree.roles.editor.editor_evidence:CarriedTraces` | |
| `evolution.tree.editor_evidence:_scoring_records` → `evolution.tree.roles.editor.editor_evidence:_scoring_records` | |
| `evolution.tree.editor_evidence:project_observations` → `evolution.tree.roles.editor.editor_evidence:project_observations` | |
| `evolution.tree.editor_evidence:_verified_view` → `evolution.tree.roles.editor.editor_evidence:_verified_view` | |
| `evolution.tree.editor_evidence:_observation` → `evolution.tree.roles.editor.editor_evidence:_observation` | |
| `evolution.tree.editor_evidence:read_trace` → `evolution.tree.roles.editor.editor_evidence:read_trace` | |
| `evolution.tree.editor_evidence:_bound_view` → `evolution.tree.roles.editor.editor_evidence:_bound_view` | |
| `evolution.tree.editor_evidence:_read_and_bind` → `evolution.tree.roles.editor.editor_evidence:_read_and_bind` | |
| `evolution.tree.editor_evidence:_turns` → `evolution.tree.roles.editor.editor_evidence:_turns` | |
| `evolution.tree.campaign_editor:BRIEF_VERSION` → `evolution.tree.roles.editor_contracts:BRIEF_VERSION` | |
| `evolution.tree.campaign_editor:BRIEF_VERSION_V16` → `evolution.tree.roles.editor_contracts:BRIEF_VERSION_V16` | |
| `evolution.tree.campaign_editor:BRIEF_FEEDBACK_VERSION_V16` → `evolution.tree.roles.editor_contracts:BRIEF_FEEDBACK_VERSION_V16` | |
| `evolution.tree.campaign_editor:BRIEF_LIMITS_V2` → `evolution.tree.roles.editor_contracts:BRIEF_LIMITS_V2` | |
| `evolution.tree.campaign_editor:BRIEF_LIMITS_V3` → `evolution.tree.roles.editor_contracts:BRIEF_LIMITS_V3` | |
| `evolution.tree.campaign_editor:PRESENT` → `evolution.tree.roles.editor_contracts:PRESENT` | |
| `evolution.tree.campaign_editor:ABSENT` → `evolution.tree.roles.editor_contracts:ABSENT` | |
| `evolution.tree.campaign_editor:UNAVAILABLE` → `evolution.tree.roles.editor_contracts:UNAVAILABLE` | |
| `evolution.tree.campaign_editor:WITHHELD` → `evolution.tree.roles.editor_contracts:WITHHELD` | |
| `evolution.tree.campaign_editor:EDIT_EXCERPT_CHARS` → `evolution.tree.roles.editor_contracts:EDIT_EXCERPT_CHARS` | |
| `evolution.tree.campaign_editor:AttemptChanges` → `evolution.tree.roles.editor_contracts:AttemptChanges` | |
| `evolution.tree.campaign_editor:AttemptResultRepeat` → `evolution.tree.roles.editor_contracts:AttemptResultRepeat` | |
| `evolution.tree.campaign_editor:AttemptResultCase` → `evolution.tree.roles.editor_contracts:AttemptResultCase` | |
| `evolution.tree.campaign_editor:AttemptResult` → `evolution.tree.roles.editor_contracts:AttemptResult` | |
| `evolution.tree.campaign_editor:HistoryAttempt` → `evolution.tree.roles.editor_contracts:HistoryAttempt` | |
| `evolution.tree.campaign_editor:HistoryRound` → `evolution.tree.roles.editor_contracts:HistoryRound` | |
| `evolution.tree.campaign_editor:BriefHistory` → `evolution.tree.roles.editor_contracts:BriefHistory` | |
| `evolution.tree.campaign_editor:EMPTY_HISTORY` → `evolution.tree.roles.editor_contracts:EMPTY_HISTORY` | |
| `evolution.tree.campaign_editor:_diagnosis_for` → `evolution.tree.roles.editor_contracts:_diagnosis_for` | |
| `evolution.tree.campaign_editor:history_attempt_payload` → `evolution.tree.roles.editor_contracts:history_attempt_payload` | |
| `evolution.tree.campaign_editor:history_payload` → `evolution.tree.roles.editor_contracts:history_payload` | |
| `evolution.tree.campaign_editor:ObservationInput` → `evolution.tree.roles.editor_contracts:ObservationInput` | |
| `evolution.tree.campaign_editor:ObservationTurn` → `evolution.tree.roles.editor_contracts:ObservationTurn` | |
| `evolution.tree.campaign_editor:Observation` → `evolution.tree.roles.editor_contracts:Observation` | |
| `evolution.tree.campaign_editor:ABSENT_OBSERVATION` → `evolution.tree.roles.editor_contracts:ABSENT_OBSERVATION` | |
| `evolution.tree.campaign_editor:UNAVAILABLE_OBSERVATION` → `evolution.tree.roles.editor_contracts:UNAVAILABLE_OBSERVATION` | |
| `evolution.tree.campaign_editor:observation_payload` → `evolution.tree.roles.editor_contracts:observation_payload` | |
| `evolution.tree.campaign_editor:_REFLECTOR_OBSERVATION_KEYS` → `evolution.tree.roles.editor_contracts:_REFLECTOR_OBSERVATION_KEYS` | |
| `evolution.tree.campaign_editor:_REFLECTOR_TURN_KEYS` → `evolution.tree.roles.editor_contracts:_REFLECTOR_TURN_KEYS` | |
| `evolution.tree.campaign_editor:reflector_observation_payload` → `evolution.tree.roles.editor_contracts:reflector_observation_payload` | |
| `evolution.tree.campaign_editor:EditBriefRepeat` → `evolution.tree.roles.editor_contracts:EditBriefRepeat` | |
| `evolution.tree.campaign_editor:EditBriefCase` → `evolution.tree.roles.editor_contracts:EditBriefCase` | |
| `evolution.tree.campaign_editor:DiagnosisFocus` → `evolution.tree.roles.editor_contracts:DiagnosisFocus` | |
| `evolution.tree.campaign_editor:DiagnosisGroup` → `evolution.tree.roles.editor_contracts:DiagnosisGroup` | |
| `evolution.tree.campaign_editor:Diagnosis` → `evolution.tree.roles.editor_contracts:Diagnosis` | |
| `evolution.tree.campaign_editor:diagnosis_for_brief` → `evolution.tree.roles.editor_contracts:diagnosis_for_brief` | |
| `evolution.tree.campaign_editor:diagnosis_payload` → `evolution.tree.roles.editor_contracts:diagnosis_payload` | |
| `evolution.tree.campaign_editor:EditBrief` → `evolution.tree.roles.editor_contracts:EditBrief` | |
| `evolution.tree.campaign_editor:with_attempt_feedback` → `evolution.tree.roles.editor_contracts:with_attempt_feedback` | |
| `evolution.tree.campaign_editor:PROJECTION_REFUSED_NOTE` → `evolution.tree.roles.editor_contracts:PROJECTION_REFUSED_NOTE` | |
| `evolution.tree.campaign_editor:PUBLICATION_REFUSED_NOTE` → `evolution.tree.roles.editor_contracts:PUBLICATION_REFUSED_NOTE` | |
| `evolution.tree.campaign_editor:RoleProjection` → `evolution.tree.roles.editor_contracts:RoleProjection` | |
| `evolution.tree.campaign_editor:EditRequest` → `evolution.tree.roles.editor_contracts:EditRequest` | |
| `evolution.tree.campaign_editor:EditResult` → `evolution.tree.roles.editor_contracts:EditResult` | |
| `evolution.tree.campaign_editor:outcome_class` → `evolution.tree.roles.editor_contracts:outcome_class` | |
| `evolution.tree.campaign_editor:CandidateEditor` → `evolution.tree.roles.editor_contracts:CandidateEditor` | |
| `evolution.tree.campaign_editor:plain_public` → `evolution.tree.roles.editor_contracts:plain_public` | |
| `evolution.tree.campaign_editor:brief_payload` → `evolution.tree.roles.editor_contracts:brief_payload` | |
| `evolution.tree.campaign_editor:_brief_payload` → `evolution.tree.roles.editor_contracts:_brief_payload` | |
| `evolution.tree.campaign_editor:brief_digest` → `evolution.tree.roles.editor_contracts:brief_digest` | |
| `evolution.tree.campaign_editor:note_digest` → `evolution.tree.roles.editor_contracts:note_digest` | |
| `evolution.tree.campaign_editor:brief_version_for` → `evolution.tree.roles.editor_contracts:brief_version_for` | |
| `evolution.tree.campaign_editor:cited_evidence_ids` → `evolution.tree.roles.editor_contracts:cited_evidence_ids` | |
| `evolution.tree.campaign_editor:cite_observations` → `evolution.tree.roles.editor_contracts:cite_observations` | |
| `evolution.tree.campaign_reflector:REFLECT_REQUEST_VERSION` → `evolution.tree.roles.reflector_contracts:REFLECT_REQUEST_VERSION` | |
| `evolution.tree.campaign_reflector:REFLECT_TOOL_SCHEMA` → `evolution.tree.roles.reflector_contracts:REFLECT_TOOL_SCHEMA` | |
| `evolution.tree.campaign_reflector:MIN_NEEDLE_LENGTH` → `evolution.tree.roles.reflector_contracts:MIN_NEEDLE_LENGTH` | |
| `evolution.tree.campaign_reflector:_OBSERVATION_LIMIT_KEYS` → `evolution.tree.roles.reflector_contracts:_OBSERVATION_LIMIT_KEYS` | |
| `evolution.tree.campaign_reflector:REFLECT_LIMITS_V1` → `evolution.tree.roles.reflector_contracts:REFLECT_LIMITS_V1` | |
| `evolution.tree.campaign_reflector:HISTORY_MAX_CHARS` → `evolution.tree.roles.reflector_contracts:HISTORY_MAX_CHARS` | |
| `evolution.tree.campaign_reflector:SUMMARY_WINDOW_ROUNDS` → `evolution.tree.roles.reflector_contracts:SUMMARY_WINDOW_ROUNDS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_PATHS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_PATHS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_CHANGES` → `evolution.tree.roles.reflector_contracts:AGGREGATE_CHANGES` | |
| `evolution.tree.campaign_reflector:AGGREGATE_CHANGE_WITNESSES` → `evolution.tree.roles.reflector_contracts:AGGREGATE_CHANGE_WITNESSES` | |
| `evolution.tree.campaign_reflector:AGGREGATE_REASONS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_REASONS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_PROMOTIONS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_PROMOTIONS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_GAINS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_GAINS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_RECENT_ROUNDS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_RECENT_ROUNDS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_PROMOTION_PATHS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_PROMOTION_PATHS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_ROUND_OUTCOMES` → `evolution.tree.roles.reflector_contracts:AGGREGATE_ROUND_OUTCOMES` | |
| `evolution.tree.campaign_reflector:REPEAT_MIN_ROUNDS` → `evolution.tree.roles.reflector_contracts:REPEAT_MIN_ROUNDS` | |
| `evolution.tree.campaign_reflector:PATH_LITERAL_MAX_CHARS` → `evolution.tree.roles.reflector_contracts:PATH_LITERAL_MAX_CHARS` | |
| `evolution.tree.campaign_reflector:CHANGE_EXCERPT_MAX_CHARS` → `evolution.tree.roles.reflector_contracts:CHANGE_EXCERPT_MAX_CHARS` | |
| `evolution.tree.campaign_reflector:CHANGE_FINGERPRINT_SCHEME` → `evolution.tree.roles.reflector_contracts:CHANGE_FINGERPRINT_SCHEME` | |
| `evolution.tree.campaign_reflector:FINGERPRINT_MAX_LINES` → `evolution.tree.roles.reflector_contracts:FINGERPRINT_MAX_LINES` | |
| `evolution.tree.campaign_reflector:FINGERPRINT_MAX_BYTES` → `evolution.tree.roles.reflector_contracts:FINGERPRINT_MAX_BYTES` | |
| `evolution.tree.campaign_reflector:ATTEMPT_CHANGES_MAX_CHARS` → `evolution.tree.roles.reflector_contracts:ATTEMPT_CHANGES_MAX_CHARS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_MAX_CHARS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_MAX_CHARS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_REDUCED_MAX_CHARS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_REDUCED_MAX_CHARS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_FULL_LIMITS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_FULL_LIMITS` | |
| `evolution.tree.campaign_reflector:AGGREGATE_REDUCED_LIMITS` → `evolution.tree.roles.reflector_contracts:AGGREGATE_REDUCED_LIMITS` | |
| `evolution.tree.campaign_reflector:COMPACT_TEXT_CHARS` → `evolution.tree.roles.reflector_contracts:COMPACT_TEXT_CHARS` | |
| `evolution.tree.campaign_reflector:COMPACT_TARGET_PATHS` → `evolution.tree.roles.reflector_contracts:COMPACT_TARGET_PATHS` | |
| `evolution.tree.campaign_reflector:COMPACT_CHANGED_PATHS` → `evolution.tree.roles.reflector_contracts:COMPACT_CHANGED_PATHS` | |
| `evolution.tree.campaign_reflector:GROUP_KEYS` → `evolution.tree.roles.reflector_contracts:GROUP_KEYS` | |
| `evolution.tree.campaign_reflector:GROUP_KEYS_WITH_PROBES` → `evolution.tree.roles.reflector_contracts:GROUP_KEYS_WITH_PROBES` | |
| `evolution.tree.campaign_reflector:ProbeAccess` → `evolution.tree.roles.reflector_contracts:ProbeAccess` | |
| `evolution.tree.campaign_reflector:ReflectRequest` → `evolution.tree.roles.reflector_contracts:ReflectRequest` | |
| `evolution.tree.campaign_reflector:RejectionDetail` → `evolution.tree.roles.reflector_contracts:RejectionDetail` | |
| `evolution.tree.campaign_reflector:ReflectResult` → `evolution.tree.roles.reflector_contracts:ReflectResult` | |
| `evolution.tree.campaign_reflector:CandidateReflector` → `evolution.tree.roles.reflector_contracts:CandidateReflector` | |
| `evolution.tree.campaign_reflector:Group` → `evolution.tree.roles.reflector_contracts:Group` | |
| `evolution.tree.campaign_reflector:GroupError` → `evolution.tree.roles.reflector_contracts:GroupError` | |
| `evolution.tree.campaign_reflector:oracle_strings` → `evolution.tree.roles.reflector_contracts:oracle_strings` | |
| `evolution.tree.campaign_reflector:oracle_needles` → `evolution.tree.roles.reflector_contracts:oracle_needles` | |
| `evolution.tree.campaign_reflector:editor_visible_identifiers` → `evolution.tree.roles.reflector_contracts:editor_visible_identifiers` | |
| `evolution.tree.campaign_reflector:_is_observation_id` → `evolution.tree.roles.reflector_contracts:_is_observation_id` | |
| `evolution.tree.campaign_reflector:reflection_needles` → `evolution.tree.roles.reflector_contracts:reflection_needles` | |
| `evolution.tree.campaign_reflector:first_needle` → `evolution.tree.roles.reflector_contracts:first_needle` | |
| `evolution.tree.campaign_reflector:find_needle` → `evolution.tree.roles.reflector_contracts:find_needle` | |
| `evolution.tree.campaign_reflector:NeedleWitness` → `evolution.tree.roles.reflector_contracts:NeedleWitness` | |
| `evolution.tree.campaign_reflector:locate_needle` → `evolution.tree.roles.reflector_contracts:locate_needle` | |
| `evolution.tree.campaign_reflector:encode_pointer` → `evolution.tree.roles.reflector_contracts:encode_pointer` | |
| `evolution.tree.campaign_reflector:_sentence` → `evolution.tree.roles.reflector_contracts:_sentence` | |
| `evolution.tree.campaign_reflector:_strings` → `evolution.tree.roles.reflector_contracts:_strings` | |
| `evolution.tree.campaign_reflector:_is_candidate_path` → `evolution.tree.roles.reflector_contracts:_is_candidate_path` | |
| `evolution.tree.campaign_reflector:_matches_any` → `evolution.tree.roles.reflector_contracts:_matches_any` | |
| `evolution.tree.campaign_reflector:validate_groups` → `evolution.tree.roles.reflector_contracts:validate_groups` | |
| `evolution.tree.campaign_reflector:_screen_leaks` → `evolution.tree.roles.reflector_contracts:_screen_leaks` | |
| `evolution.tree.campaign_reflector:admit_reflection_result` → `evolution.tree.roles.reflector_contracts:admit_reflection_result` | |
| `evolution.tree.campaign_reflector:result_note_digest` → `evolution.tree.roles.reflector_contracts:result_note_digest` | |
| `evolution.tree.campaign_reflector:adaptive_group_schema` → `evolution.tree.roles.reflector_contracts:adaptive_group_schema` | |
| `evolution.tree.campaign_reflector:is_empty` → `evolution.tree.roles.reflector_contracts:is_empty` | |
| `evolution.tree.campaign_reflector:drop_empty_unknown_keys` → `evolution.tree.roles.reflector_contracts:drop_empty_unknown_keys` | |
| `evolution.tree.campaign_reflector:_missing_key_correction` → `evolution.tree.roles.reflector_contracts:_missing_key_correction` | |
| `evolution.tree.campaign_reflector:ADAPTIVE_GROUP_TEXT_KEYS` → `evolution.tree.roles.reflector_contracts:ADAPTIVE_GROUP_TEXT_KEYS` | |
| `evolution.tree.campaign_reflector:ADAPTIVE_GROUP_LIMITS` → `evolution.tree.roles.limits:ADAPTIVE_GROUP_LIMITS` | |
| `evolution.tree.campaign_reflector:_group_bytes` → `evolution.tree.roles.reflector_contracts:_group_bytes` | |
| `evolution.tree.campaign_reflector:_ADAPTIVE_LIST_KEYS` → `evolution.tree.roles.reflector_contracts:_ADAPTIVE_LIST_KEYS` | |
| `evolution.tree.campaign_reflector:adaptive_limits` → `evolution.tree.roles.limits:adaptive_limits` | |
| `evolution.tree.campaign_reflector:_over` → `evolution.tree.roles.reflector_contracts:_over` | |
| `evolution.tree.campaign_reflector:adaptive_group_violation` → `evolution.tree.roles.reflector_contracts:adaptive_group_violation` | |
| `evolution.tree.campaign_reflector:_as_stored` → `evolution.tree.roles.reflector_contracts:_as_stored` | |
| `evolution.tree.campaign_reflector:check_adaptive_group_bounds` → `evolution.tree.roles.reflector_contracts:check_adaptive_group_bounds` | |
| `evolution.tree.campaign_reflector:_validate_adaptive_groups` → `evolution.tree.roles.reflector_contracts:_validate_adaptive_groups` | |
| `evolution.tree.campaign_reflector:REFLECT_TOOL_SCHEMA_V16` → `evolution.tree.roles.reflector_contracts:REFLECT_TOOL_SCHEMA_V16` | |
| `evolution.tree.campaign_reflector:reflect_tool_schema_for` → `evolution.tree.roles.reflector_contracts:reflect_tool_schema_for` | |
| `evolution.tree.campaign_identity:EditorIdentity` → `evolution.tree.roles.identities:EditorIdentity` | |
| `evolution.tree.campaign_identity:ReflectorIdentity` → `evolution.tree.roles.identities:ReflectorIdentity` | |
| `evolution.tree.campaign_editor:_brief_limits` → `evolution.tree.roles.editor_contracts:_brief_limits` | |
| `evolution.tree.campaign_editor:EditorIdentity` → `evolution.tree.roles.identities:EditorIdentity` | |

### Manifest package import migration

| Old path → new defining path | Consumer |
|---|---|
| `evolution.tree.manifest_v2:SCHEMA_V15` → `evolution.tree.manifest.manifest_v2:SCHEMA_V15` | |
| `evolution.tree.manifest_v2:SCHEMA_V16` → `evolution.tree.manifest.manifest_v2:SCHEMA_V16` | |
| `evolution.tree.manifest_v2:DEFAULT_MAX_RESPONSE_BYTES` → `evolution.tree.target.config:DEFAULT_MAX_RESPONSE_BYTES` | |
| `evolution.tree.manifest_v2:DEFAULT_MIN_INPUT_BYTES` → `evolution.tree.target.config:DEFAULT_MIN_INPUT_BYTES` | |
| `evolution.tree.manifest_v2:DEFAULT_CONNECT_SECONDS` → `evolution.tree.target.config:DEFAULT_CONNECT_SECONDS` | |
| `evolution.tree.manifest_v2:DEFAULT_READ_SECONDS` → `evolution.tree.target.config:DEFAULT_READ_SECONDS` | |
| `evolution.tree.manifest_v2:DEFAULT_INPUT_TEXT_KEY` → `evolution.tree.execution.config:DEFAULT_INPUT_TEXT_KEY` | |
| `evolution.tree.manifest_v2:_LOOPBACK_HOSTS` → `evolution.tree.target.config:_LOOPBACK_HOSTS` | |
| `evolution.tree.manifest_v2:_HOSTNAME_LABEL_RE` → `evolution.tree.manifest.manifest_v2:_HOSTNAME_LABEL_RE` | |
| `evolution.tree.manifest_v2:_ROOT` → `evolution.tree.manifest.manifest_v2:_ROOT` | |
| `evolution.tree.manifest_v2:_DOCUMENT` → `evolution.tree.manifest.manifest_v2:_DOCUMENT` | |
| `evolution.tree.manifest_v2:_CONFORMANCE_KEYS` → `evolution.tree.manifest.manifest_v2:_CONFORMANCE_KEYS` | |
| `evolution.tree.manifest_v2:_TOP_LEVEL_KEYS_V15` → `evolution.tree.manifest.manifest_v2:_TOP_LEVEL_KEYS_V15` | |
| `evolution.tree.manifest_v2:_OPTIONAL_TOP_LEVEL_KEYS_V15` → `evolution.tree.manifest.manifest_v2:_OPTIONAL_TOP_LEVEL_KEYS_V15` | |
| `evolution.tree.manifest_v2:_OPTIONAL_TOP_LEVEL_KEYS_V16` → `evolution.tree.manifest.manifest_v2:_OPTIONAL_TOP_LEVEL_KEYS_V16` | |
| `evolution.tree.manifest_v2:_LINKED_FROM_V6_REQUIRED` → `evolution.tree.manifest.manifest_v2:_LINKED_FROM_V6_REQUIRED` | |
| `evolution.tree.manifest_v2:_LINKED_FROM_V6_BASELINES` → `evolution.tree.manifest.manifest_v2:_LINKED_FROM_V6_BASELINES` | |
| `evolution.tree.manifest_v2:_TARGET_REQUIRED` → `evolution.tree.manifest.manifest_v2:_TARGET_REQUIRED` | |
| `evolution.tree.manifest_v2:_TARGET_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_TARGET_OPTIONAL` | |
| `evolution.tree.manifest_v2:_TIMEOUTS_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_TIMEOUTS_OPTIONAL` | |
| `evolution.tree.manifest_v2:_ARTIFACTS_REQUIRED` → `evolution.tree.manifest.manifest_v2:_ARTIFACTS_REQUIRED` | |
| `evolution.tree.manifest_v2:_ENVIRONMENT_KINDS` → `evolution.tree.manifest.manifest_v2:_ENVIRONMENT_KINDS` | |
| `evolution.tree.manifest_v2:_ENVIRONMENT_REQUIRED` → `evolution.tree.manifest.manifest_v2:_ENVIRONMENT_REQUIRED` | |
| `evolution.tree.manifest_v2:_ENVIRONMENT_ALLOWED` → `evolution.tree.manifest.manifest_v2:_ENVIRONMENT_ALLOWED` | |
| `evolution.tree.manifest_v2:_COMMAND_REQUIRED` → `evolution.tree.manifest.manifest_v2:_COMMAND_REQUIRED` | |
| `evolution.tree.manifest_v2:_CORPUS_REQUIRED` → `evolution.tree.manifest.manifest_v2:_CORPUS_REQUIRED` | |
| `evolution.tree.manifest_v2:EDITOR_KINDS` → `evolution.tree.manifest.manifest_v2:EDITOR_KINDS` | |
| `evolution.tree.manifest_v2:_EDITOR_KINDS` → `evolution.tree.manifest.manifest_v2:_EDITOR_KINDS` | |
| `evolution.tree.manifest_v2:REASONING_EFFORTS` → `evolution.tree.manifest.manifest_v2:REASONING_EFFORTS` | |
| `evolution.tree.manifest_v2:_CHAT_REQUIRED` → `evolution.tree.manifest.manifest_v2:_CHAT_REQUIRED` | |
| `evolution.tree.manifest_v2:_CHAT_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_CHAT_OPTIONAL` | |
| `evolution.tree.manifest_v2:DEFAULT_CONTEXT_COMPACT_CHARS` → `evolution.tree.roles.config:DEFAULT_CONTEXT_COMPACT_CHARS` | |
| `evolution.tree.manifest_v2:DEFAULT_CONTEXT_MESSAGE_MAX_CHARS` → `evolution.tree.roles.config:DEFAULT_CONTEXT_MESSAGE_MAX_CHARS` | |
| `evolution.tree.manifest_v2:_AGENT_REQUIRED` → `evolution.tree.manifest.manifest_v2:_AGENT_REQUIRED` | |
| `evolution.tree.manifest_v2:_AGENT_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_AGENT_OPTIONAL` | |
| `evolution.tree.manifest_v2:REFLECTOR_KINDS` → `evolution.tree.manifest.manifest_v2:REFLECTOR_KINDS` | |
| `evolution.tree.manifest_v2:_REFLECTOR_KINDS` → `evolution.tree.manifest.manifest_v2:_REFLECTOR_KINDS` | |
| `evolution.tree.manifest_v2:_REFLECTOR_COMMON` → `evolution.tree.manifest.manifest_v2:_REFLECTOR_COMMON` | |
| `evolution.tree.manifest_v2:_CHAT_REFLECTOR_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_CHAT_REFLECTOR_OPTIONAL` | |
| `evolution.tree.manifest_v2:_AGENT_REFLECTOR_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_AGENT_REFLECTOR_OPTIONAL` | |
| `evolution.tree.manifest_v2:_PROBE_REQUIRED` → `evolution.tree.manifest.manifest_v2:_PROBE_REQUIRED` | |
| `evolution.tree.manifest_v2:_PROBE_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_PROBE_OPTIONAL` | |
| `evolution.tree.manifest_v2:_PROBE_METHODS` → `evolution.tree.manifest.manifest_v2:_PROBE_METHODS` | |
| `evolution.tree.manifest_v2:_MAX_PROBE_DESCRIPTION_BYTES` → `evolution.tree.manifest.manifest_v2:_MAX_PROBE_DESCRIPTION_BYTES` | |
| `evolution.tree.manifest_v2:_MAX_PROBE_PATH_BYTES` → `evolution.tree.target.config:_MAX_PROBE_PATH_BYTES` | |
| `evolution.tree.manifest_v2:_GROUPING_FIELDS_REQUIRED` → `evolution.tree.manifest.manifest_v2:_GROUPING_FIELDS_REQUIRED` | |
| `evolution.tree.manifest_v2:_GROUPING_COMMAND_REQUIRED` → `evolution.tree.manifest.manifest_v2:_GROUPING_COMMAND_REQUIRED` | |
| `evolution.tree.manifest_v2:_GROUPING_MODEL_REQUIRED` → `evolution.tree.manifest.manifest_v2:_GROUPING_MODEL_REQUIRED` | |
| `evolution.tree.manifest_v2:_GROUPING_MODEL_OPTIONAL` → `evolution.tree.manifest.manifest_v2:_GROUPING_MODEL_OPTIONAL` | |
| `evolution.tree.manifest_v2:_ROLE_CALL_RETRY_KEYS` → `evolution.tree.manifest.manifest_v2:_ROLE_CALL_RETRY_KEYS` | |
| `evolution.tree.manifest_v2:_SELECTION_ALLOWED` → `evolution.tree.manifest.manifest_v2:_SELECTION_ALLOWED` | |
| `evolution.tree.manifest_v2:_FAMILIES_NONE` → `evolution.tree.manifest.manifest_v2:_FAMILIES_NONE` | |
| `evolution.tree.manifest_v2:_FAMILIES_METADATA` → `evolution.tree.manifest.manifest_v2:_FAMILIES_METADATA` | |
| `evolution.tree.manifest_v2:GROUPER_KINDS` → `evolution.tree.manifest.manifest_v2:GROUPER_KINDS` | |
| `evolution.tree.manifest_v2:_GROUPER_KINDS` → `evolution.tree.manifest.manifest_v2:_GROUPER_KINDS` | |
| `evolution.tree.manifest_v2:_AGENT_GROUPER_TIMEOUT` → `evolution.tree.manifest.manifest_v2:_AGENT_GROUPER_TIMEOUT` | |
| `evolution.tree.manifest_v2:ManifestError` → `evolution.tree.execution.config:ManifestError` | |
| `evolution.tree.manifest_v2:Timeouts` → `evolution.tree.target.config:Timeouts` | |
| `evolution.tree.manifest_v2:_normalized_loopback` → `evolution.tree.target.config:_normalized_loopback` | |
| `evolution.tree.manifest_v2:TargetBinding` → `evolution.tree.target.config:TargetBinding` | |
| `evolution.tree.manifest_v2:TargetBindingV16` → `evolution.tree.target.config:TargetBindingV16` | |
| `evolution.tree.manifest_v2:EnvironmentSection` → `evolution.tree.execution.config:EnvironmentSection` | |
| `evolution.tree.manifest_v2:CommandSection` → `evolution.tree.execution.config:CommandSection` | |
| `evolution.tree.manifest_v2:_DEFAULT_BUDGETS` → `evolution.tree.roles.config:_DEFAULT_BUDGETS` | |
| `evolution.tree.manifest_v2:ChatEditorSection` → `evolution.tree.roles.config:ChatEditorSection` | |
| `evolution.tree.manifest_v2:AgentCliEditorSection` → `evolution.tree.roles.config:AgentCliEditorSection` | |
| `evolution.tree.manifest_v2:EditorSection` → `evolution.tree.roles.config:EditorSection` | |
| `evolution.tree.manifest_v2:ProbeSection` → `evolution.tree.target.config:ProbeSection` | |
| `evolution.tree.manifest_v2:FieldsGroupingSection` → `evolution.tree.roles.config:FieldsGroupingSection` | |
| `evolution.tree.manifest_v2:CommandGroupingSection` → `evolution.tree.roles.config:CommandGroupingSection` | |
| `evolution.tree.manifest_v2:ModelGroupingSection` → `evolution.tree.roles.config:ModelGroupingSection` | |
| `evolution.tree.manifest_v2:GroupingSection` → `evolution.tree.roles.config:GroupingSection` | |
| `evolution.tree.manifest_v2:AgentCliGrouperSection` → `evolution.tree.roles.config:AgentCliGrouperSection` | |
| `evolution.tree.manifest_v2:GrouperSection` → `evolution.tree.roles.config:GrouperSection` | |
| `evolution.tree.manifest_v2:SelectionSection` → `evolution.tree.roles.config:SelectionSection` | |
| `evolution.tree.manifest_v2:FamilySection` → `evolution.tree.roles.config:FamilySection` | |
| `evolution.tree.manifest_v2:ReflectorSection` → `evolution.tree.roles.config:ReflectorSection` | |
| `evolution.tree.manifest_v2:effective_protocol_repairs` → `evolution.tree.roles.config:effective_protocol_repairs` | |
| `evolution.tree.manifest_v2:effective_tool_failures` → `evolution.tree.roles.config:effective_tool_failures` | |
| `evolution.tree.manifest_v2:effective_grouper_repairs` → `evolution.tree.roles.config:effective_grouper_repairs` | |
| `evolution.tree.manifest_v2:effective_full_fallbacks` → `evolution.tree.manifest.manifest_v2:effective_full_fallbacks` | |
| `evolution.tree.manifest_v2:effective_skips` → `evolution.tree.manifest.manifest_v2:effective_skips` | |
| `evolution.tree.manifest_v2:_ABSENT_GROUP_TEXT_CHARS` → `evolution.tree.manifest.manifest_v2:_ABSENT_GROUP_TEXT_CHARS` | |
| `evolution.tree.manifest_v2:effective_group_text_chars` → `evolution.tree.manifest.manifest_v2:effective_group_text_chars` | |
| `evolution.tree.manifest_v2:WorkflowSections` → `evolution.tree.manifest.manifest_v2:WorkflowSections` | |
| `evolution.tree.manifest_v2:ManifestV2` → `evolution.tree.manifest.manifest_v2:ManifestV2` | |
| `evolution.tree.manifest_v2:_path` → `evolution.tree.manifest.manifest_v2:_path` | |
| `evolution.tree.manifest_v2:_dict_or_raise` → `evolution.tree.manifest.manifest_v2:_dict_or_raise` | |
| `evolution.tree.manifest_v2:_check_missing` → `evolution.tree.manifest.manifest_v2:_check_missing` | |
| `evolution.tree.manifest_v2:_check_unknown` → `evolution.tree.manifest.manifest_v2:_check_unknown` | |
| `evolution.tree.manifest_v2:_str_or_raise` → `evolution.tree.manifest.manifest_v2:_str_or_raise` | |
| `evolution.tree.manifest_v2:_optional_identifier` → `evolution.tree.manifest.manifest_v2:_optional_identifier` | |
| `evolution.tree.manifest_v2:_positive_int` → `evolution.tree.manifest.manifest_v2:_positive_int` | |
| `evolution.tree.manifest_v2:_positive_number_to_float` → `evolution.tree.manifest.manifest_v2:_positive_number_to_float` | |
| `evolution.tree.manifest_v2:_str_list` → `evolution.tree.manifest.manifest_v2:_str_list` | |
| `evolution.tree.manifest_v2:_validate_structure` → `evolution.tree.manifest.manifest_v2:_validate_structure` | |
| `evolution.tree.manifest_v2:_parse_timeouts` → `evolution.tree.manifest.manifest_v2:_parse_timeouts` | |
| `evolution.tree.manifest_v2:_parse_target` → `evolution.tree.manifest.manifest_v2:_parse_target` | |
| `evolution.tree.manifest_v2:_parse_artifacts` → `evolution.tree.manifest.manifest_v2:_parse_artifacts` | |
| `evolution.tree.manifest_v2:_identifier` → `evolution.tree.manifest.manifest_v2:_identifier` | |
| `evolution.tree.manifest_v2:_positive_seconds` → `evolution.tree.manifest.manifest_v2:_positive_seconds` | |
| `evolution.tree.manifest_v2:_argv` → `evolution.tree.manifest.manifest_v2:_argv` | |
| `evolution.tree.manifest_v2:_env_names` → `evolution.tree.manifest.manifest_v2:_env_names` | |
| `evolution.tree.manifest_v2:_argv_program_paths` → `evolution.tree.manifest.manifest_v2:_argv_program_paths` | |
| `evolution.tree.manifest_v2:_campaign_error` → `evolution.tree.manifest.manifest_v2:_campaign_error` | |
| `evolution.tree.manifest_v2:_parse_campaign_v11` → `evolution.tree.manifest.manifest_v2:_parse_campaign_v11` | |
| `evolution.tree.manifest_v2:_parse_campaign_v12` → `evolution.tree.manifest.manifest_v2:_parse_campaign_v12` | |
| `evolution.tree.manifest_v2:_contract_refusal` → `evolution.tree.manifest.manifest_v2:_contract_refusal` | |
| `evolution.tree.manifest_v2:_parse_scorer_v15` → `evolution.tree.manifest.manifest_v2:_parse_scorer_v15` | |
| `evolution.tree.manifest_v2:_parse_role_visibility` → `evolution.tree.manifest.manifest_v2:_parse_role_visibility` | |
| `evolution.tree.manifest_v2:_parse_verbatim_answers` → `evolution.tree.manifest.manifest_v2:_parse_verbatim_answers` | |
| `evolution.tree.manifest_v2:_parse_secret_oracle_paths` → `evolution.tree.manifest.manifest_v2:_parse_secret_oracle_paths` | |
| `evolution.tree.manifest_v2:_parse_checks` → `evolution.tree.manifest.manifest_v2:_parse_checks` | |
| `evolution.tree.manifest_v2:_parse_evidence` → `evolution.tree.manifest.manifest_v2:_parse_evidence` | |
| `evolution.tree.manifest_v2:_parse_linked_from_v6` → `evolution.tree.manifest.manifest_v2:_parse_linked_from_v6` | |
| `evolution.tree.manifest_v2:_parse_environment` → `evolution.tree.manifest.manifest_v2:_parse_environment` | |
| `evolution.tree.manifest_v2:_parse_command` → `evolution.tree.manifest.manifest_v2:_parse_command` | |
| `evolution.tree.manifest_v2:_refuse_program_inside_seed` → `evolution.tree.manifest.manifest_v2:_refuse_program_inside_seed` | |
| `evolution.tree.manifest_v2:_existing_program_file` → `evolution.tree.manifest.manifest_v2:_existing_program_file` | |
| `evolution.tree.manifest_v2:_env_name` → `evolution.tree.manifest.manifest_v2:_env_name` | |
| `evolution.tree.manifest_v2:_optional_env_name` → `evolution.tree.manifest.manifest_v2:_optional_env_name` | |
| `evolution.tree.manifest_v2:_optional_count` → `evolution.tree.manifest.manifest_v2:_optional_count` | |
| `evolution.tree.manifest_v2:_positive_count` → `evolution.tree.manifest.manifest_v2:_positive_count` | |
| `evolution.tree.manifest_v2:_bool_or_raise` → `evolution.tree.manifest.manifest_v2:_bool_or_raise` | |
| `evolution.tree.manifest_v2:_ranged_int` → `evolution.tree.manifest.manifest_v2:_ranged_int` | |
| `evolution.tree.manifest_v2:_reasoning_effort` → `evolution.tree.manifest.manifest_v2:_reasoning_effort` | |
| `evolution.tree.manifest_v2:_optional_nonempty_str_list` → `evolution.tree.manifest.manifest_v2:_optional_nonempty_str_list` | |
| `evolution.tree.manifest_v2:_provider_routing` → `evolution.tree.manifest.manifest_v2:_provider_routing` | |
| `evolution.tree.manifest_v2:_is_syntactically_valid_hostname` → `evolution.tree.manifest.manifest_v2:_is_syntactically_valid_hostname` | |
| `evolution.tree.manifest_v2:_chat_base_url` → `evolution.tree.manifest.manifest_v2:_chat_base_url` | |
| `evolution.tree.manifest_v2:_parse_editor_budgets` → `evolution.tree.manifest.manifest_v2:_parse_editor_budgets` | |
| `evolution.tree.manifest_v2:_parse_chat_editor` → `evolution.tree.manifest.manifest_v2:_parse_chat_editor` | |
| `evolution.tree.manifest_v2:_parse_agent_cli_editor` → `evolution.tree.manifest.manifest_v2:_parse_agent_cli_editor` | |
| `evolution.tree.manifest_v2:_parse_editor` → `evolution.tree.manifest.manifest_v2:_parse_editor` | |
| `evolution.tree.manifest_v2:_parse_environment_docs` → `evolution.tree.manifest.manifest_v2:_parse_environment_docs` | |
| `evolution.tree.manifest_v2:_validate_probe_path` → `evolution.tree.target.config:_validate_probe_path` | |
| `evolution.tree.manifest_v2:_probe_health_path` → `evolution.tree.manifest.manifest_v2:_probe_health_path` | |
| `evolution.tree.manifest_v2:_probe_methods` → `evolution.tree.manifest.manifest_v2:_probe_methods` | |
| `evolution.tree.manifest_v2:_probe_description` → `evolution.tree.manifest.manifest_v2:_probe_description` | |
| `evolution.tree.manifest_v2:_parse_probe_config` → `evolution.tree.manifest.manifest_v2:_parse_probe_config` | |
| `evolution.tree.manifest_v2:_parse_probe` → `evolution.tree.manifest.manifest_v2:_parse_probe` | |
| `evolution.tree.manifest_v2:_parse_reflection_config` → `evolution.tree.manifest.manifest_v2:_parse_reflection_config` | |
| `evolution.tree.manifest_v2:_json_depth` → `evolution.tree.manifest.manifest_v2:_json_depth` | |
| `evolution.tree.manifest_v2:_parse_fields_grouping` → `evolution.tree.manifest.manifest_v2:_parse_fields_grouping` | |
| `evolution.tree.manifest_v2:_parse_role_call_retry` → `evolution.tree.manifest.manifest_v2:_parse_role_call_retry` | |
| `evolution.tree.manifest_v2:_parse_grouping_fallback` → `evolution.tree.manifest.manifest_v2:_parse_grouping_fallback` | |
| `evolution.tree.manifest_v2:_parse_model_grouping` → `evolution.tree.manifest.manifest_v2:_parse_model_grouping` | |
| `evolution.tree.manifest_v2:_parse_command_grouping` → `evolution.tree.manifest.manifest_v2:_parse_command_grouping` | |
| `evolution.tree.manifest_v2:_parse_selection` → `evolution.tree.manifest.manifest_v2:_parse_selection` | |
| `evolution.tree.manifest_v2:_parse_families` → `evolution.tree.manifest.manifest_v2:_parse_families` | |
| `evolution.tree.manifest_v2:_parse_reflector` → `evolution.tree.manifest.manifest_v2:_parse_reflector` | |
| `evolution.tree.manifest_v2:_parse_agent_cli_grouper` → `evolution.tree.manifest.manifest_v2:_parse_agent_cli_grouper` | |
| `evolution.tree.manifest_v2:_parse_grouper_section` → `evolution.tree.manifest.manifest_v2:_parse_grouper_section` | |
| `evolution.tree.manifest_v2:_parse_grouper` → `evolution.tree.manifest.manifest_v2:_parse_grouper` | |
| `evolution.tree.manifest_v2:_parse_corpus` → `evolution.tree.manifest.manifest_v2:_parse_corpus` | |
| `evolution.tree.manifest_v2:_declared_spellings` → `evolution.tree.manifest.manifest_v2:_declared_spellings` | |
| `evolution.tree.manifest_v2:_reject_constant` → `evolution.tree.manifest.manifest_v2:_reject_constant` | |
| `evolution.tree.manifest_v2:_resolve_seed_root` → `evolution.tree.manifest.manifest_v2:_resolve_seed_root` | |
| `evolution.tree.manifest_v2:load_manifest_v2` → `evolution.tree.manifest.manifest_v2:load_manifest_v2` | |
| `evolution.tree.manifest_v2:conformance_manifest` → `evolution.tree.manifest.manifest_v2:conformance_manifest` | |
| `evolution.tree.manifest_v2:_load_manifest` → `evolution.tree.manifest.manifest_v2:_load_manifest` | |
| `evolution.tree.manifest_inventory:DependencyRole` → `evolution.tree.manifest.manifest_inventory:DependencyRole` | |
| `evolution.tree.manifest_inventory:DependencyKind` → `evolution.tree.manifest.manifest_inventory:DependencyKind` | |
| `evolution.tree.manifest_inventory:ResolutionBase` → `evolution.tree.manifest.manifest_inventory:ResolutionBase` | |
| `evolution.tree.manifest_inventory:EnvironmentRole` → `evolution.tree.manifest.manifest_inventory:EnvironmentRole` | |
| `evolution.tree.manifest_inventory:Sensitivity` → `evolution.tree.manifest.manifest_inventory:Sensitivity` | |
| `evolution.tree.manifest_inventory:BoundaryCode` → `evolution.tree.manifest.manifest_inventory:BoundaryCode` | |
| `evolution.tree.manifest_inventory:__all__` → `evolution.tree.manifest.manifest_inventory:__all__` | |
| `evolution.tree.manifest_inventory:Dependency` → `evolution.tree.manifest.manifest_inventory:Dependency` | |
| `evolution.tree.manifest_inventory:EnvironmentName` → `evolution.tree.manifest.manifest_inventory:EnvironmentName` | |
| `evolution.tree.manifest_inventory:DependencyInventory` → `evolution.tree.manifest.manifest_inventory:DependencyInventory` | |
| `evolution.tree.manifest_inventory:_Walk` → `evolution.tree.manifest.manifest_inventory:_Walk` | |
| `evolution.tree.manifest_inventory:manifest_dependencies` → `evolution.tree.manifest.manifest_inventory:manifest_dependencies` | |
| `evolution.tree.manifest_v15_help:VERBATIM_ANSWERS_EXPECTED` → `evolution.tree.manifest.manifest_v15_help:VERBATIM_ANSWERS_EXPECTED` | |
| `evolution.tree.manifest_v15_help:SECRET_ORACLE_PATHS_EXPECTED` → `evolution.tree.manifest.manifest_v15_help:SECRET_ORACLE_PATHS_EXPECTED` | |
| `evolution.tree.manifest_v15_help:_INDEX` → `evolution.tree.manifest.manifest_v15_help:_INDEX` | |
| `evolution.tree.manifest_v15_help:CONNECTION_EXPECTED` → `evolution.tree.manifest.manifest_v15_help:CONNECTION_EXPECTED` | |
| `evolution.tree.manifest_v15_help:CONNECTION_UNKNOWN` → `evolution.tree.manifest.manifest_v15_help:CONNECTION_UNKNOWN` | |
| `evolution.tree.manifest_v15_help:_normal` → `evolution.tree.manifest.manifest_v15_help:_normal` | |
| `evolution.tree.manifest_v15_help:connection_expected` → `evolution.tree.manifest.manifest_v15_help:connection_expected` | |
| `evolution.tree.manifest_v15_help:GROUPER_SECTION` → `evolution.tree.manifest.manifest_v15_help:GROUPER_SECTION` | |
| `evolution.tree.manifest_v15_help:TOP_LEVEL_V15_EXPECTED` → `evolution.tree.manifest.manifest_v15_help:TOP_LEVEL_V15_EXPECTED` | |
| `evolution.tree.manifest_v15_help:REFLECTOR_GROUPING_V15` → `evolution.tree.manifest.manifest_v15_help:REFLECTOR_GROUPING_V15` | |
| `evolution.tree.manifest_v15_help:REFLECTOR_GROUP_TEXT_CHARS` → `evolution.tree.manifest.manifest_v15_help:REFLECTOR_GROUP_TEXT_CHARS` | |
| `evolution.tree.manifest_v15_help:GROUPER_KINDS_TEXT` → `evolution.tree.manifest.manifest_v15_help:GROUPER_KINDS_TEXT` | |
| `evolution.tree.manifest_v15_help:GROUPER_KIND` → `evolution.tree.manifest.manifest_v15_help:GROUPER_KIND` | |
| `evolution.tree.manifest_v15_help:GROUPER_MODEL_KIND` → `evolution.tree.manifest.manifest_v15_help:GROUPER_MODEL_KIND` | |
| `evolution.tree.manifest_v15_help:GROUPER_TIMEOUT` → `evolution.tree.manifest.manifest_v15_help:GROUPER_TIMEOUT` | |
| `evolution.tree.manifest_v15_help:GROUPER_COMMAND_TIMEOUT` → `evolution.tree.manifest.manifest_v15_help:GROUPER_COMMAND_TIMEOUT` | |
| `evolution.tree.manifest_v15_help:GROUPER_FALLBACK` → `evolution.tree.manifest.manifest_v15_help:GROUPER_FALLBACK` | |
| `evolution.tree.manifest_v15_help:GROUPER_BY_KIND` → `evolution.tree.manifest.manifest_v15_help:GROUPER_BY_KIND` | |
| `evolution.tree.manifest_v15_help:_MANIFEST_CODES` → `evolution.tree.manifest.manifest_v15_help:_MANIFEST_CODES` | |
| `evolution.tree.manifest_v15_help:grouper_expected` → `evolution.tree.manifest.manifest_v15_help:grouper_expected` | |
| `evolution.tree.manifest_v15_help:COMPACTION_EXPECTED` → `evolution.tree.manifest.manifest_v15_help:COMPACTION_EXPECTED` | |
| `evolution.tree.manifest_v15_help:EVIDENCE_EXPECTED` → `evolution.tree.manifest.manifest_v15_help:EVIDENCE_EXPECTED` | |
| `evolution.tree.manifest_v15_help:CHECKS_EXPECTED` → `evolution.tree.manifest.manifest_v15_help:CHECKS_EXPECTED` | |
| `evolution.tree.campaign_policy_help:_FRACTION` → `evolution.tree.manifest.campaign_policy_help:_FRACTION` | |
| `evolution.tree.campaign_policy_help:_OBSERVE_NO_CONFIDENCE` → `evolution.tree.manifest.campaign_policy_help:_OBSERVE_NO_CONFIDENCE` | |
| `evolution.tree.campaign_policy_help:EXPECTED` → `evolution.tree.manifest.campaign_policy_help:EXPECTED` | |
| `evolution.tree.campaign_policy_help:UNKNOWN` → `evolution.tree.manifest.campaign_policy_help:UNKNOWN` | |
| `evolution.tree.campaign_policy_help:OBSERVE_UNKNOWN` → `evolution.tree.manifest.campaign_policy_help:OBSERVE_UNKNOWN` | |
| `evolution.tree.campaign_policy_help:OBSERVE_FORBIDDEN` → `evolution.tree.manifest.campaign_policy_help:OBSERVE_FORBIDDEN` | |
| `evolution.tree.campaign_policy_help:LEGACY_KEYS` → `evolution.tree.manifest.campaign_policy_help:LEGACY_KEYS` | |
| `evolution.tree.campaign_policy_help:OBSERVE_GLOBAL` → `evolution.tree.manifest.campaign_policy_help:OBSERVE_GLOBAL` | |
| `evolution.tree.campaign_policy_help:SCHEMA_V14_NAME` → `evolution.tree.manifest.campaign_policy_help:SCHEMA_V14_NAME` | |
| `evolution.tree.campaign_policy_help:SCHEMA_V15_NAME` → `evolution.tree.manifest.campaign_policy_help:SCHEMA_V15_NAME` | |
| `evolution.tree.campaign_policy_help:SCHEMA_V16_NAME` → `evolution.tree.manifest.campaign_policy_help:SCHEMA_V16_NAME` | |
| `evolution.tree.campaign_policy_help:EXPECTED_V15` → `evolution.tree.manifest.campaign_policy_help:EXPECTED_V15` | |
| `evolution.tree.campaign_policy_help:UNKNOWN_V15` → `evolution.tree.manifest.campaign_policy_help:UNKNOWN_V15` | |
| `evolution.tree.campaign_policy_help:OBSERVE_UNKNOWN_V15` → `evolution.tree.manifest.campaign_policy_help:OBSERVE_UNKNOWN_V15` | |
| `evolution.tree.campaign_policy_help:OBSERVE_FORBIDDEN_V15` → `evolution.tree.manifest.campaign_policy_help:OBSERVE_FORBIDDEN_V15` | |
| `evolution.tree.campaign_policy_help:LEGACY_KEYS_V15` → `evolution.tree.manifest.campaign_policy_help:LEGACY_KEYS_V15` | |
| `evolution.tree.campaign_policy_help:ROUND_STOP_PARALLEL` → `evolution.tree.manifest.campaign_policy_help:ROUND_STOP_PARALLEL` | |
| `evolution.tree.campaign_policy_help:EXPECTED_V16` → `evolution.tree.manifest.campaign_policy_help:EXPECTED_V16` | |
| `evolution.tree.campaign_policy_help:_tables` → `evolution.tree.manifest.campaign_policy_help:_tables` | |
| `evolution.tree.campaign_policy_help:expected_text` → `evolution.tree.manifest.campaign_policy_help:expected_text` | |
| `evolution.tree.campaign_policy_help:ParameterHelp` → `evolution.tree.manifest.campaign_policy_help:ParameterHelp` | |
| `evolution.tree.campaign_policy_help:PARAMETER_HELP` → `evolution.tree.manifest.campaign_policy_help:PARAMETER_HELP` | |
| `evolution.tree.campaign_policy_help:_HELP_BY_PATH` → `evolution.tree.manifest.campaign_policy_help:_HELP_BY_PATH` | |
| `evolution.tree.campaign_policy_help:help_for` → `evolution.tree.manifest.campaign_policy_help:help_for` | |
| `evolution.tree.campaign_policy_help:PARAMETER_HELP_V15` → `evolution.tree.manifest.campaign_policy_help:PARAMETER_HELP_V15` | |
| `evolution.tree.campaign_policy_help:_HELP_V15_BY_PATH` → `evolution.tree.manifest.campaign_policy_help:_HELP_V15_BY_PATH` | |
| `evolution.tree.campaign_policy_help:help_for_v15` → `evolution.tree.manifest.campaign_policy_help:help_for_v15` | |
| `evolution.tree.campaign_policy_help:_TABLE_COLUMNS` → `evolution.tree.manifest.campaign_policy_help:_TABLE_COLUMNS` | |
| `evolution.tree.campaign_policy_help:_cell` → `evolution.tree.manifest.campaign_policy_help:_cell` | |
| `evolution.tree.campaign_policy_help:_render` → `evolution.tree.manifest.campaign_policy_help:_render` | |
| `evolution.tree.campaign_policy_help:render_v14_parameter_table` → `evolution.tree.manifest.campaign_policy_help:render_v14_parameter_table` | |
| `evolution.tree.campaign_policy_help:render_v15_parameter_table` → `evolution.tree.manifest.campaign_policy_help:render_v15_parameter_table` | |
| `evolution.tree.manifest_v2:PolicyError` → `evolution.tree.campaign_policy:PolicyError` | |

### Upgrade

- **`gate.stages` is opt-in.** An absent key keeps `2.0.0b14` behavior and bytes. Adding, removing or changing `stages` on an unfinished run is a policy change: the refusal offers `rebaseline` and `close`, or only `close` when no round is left. `2.0.0b14` refuses a manifest with the key as `unknown_key`.

- **Upgrade to `2.0.0b15`.** Install the same release at both wire ends. An unfinished `/15` or `/16` run is refused once with `run_identity_changed`, effect `rebase`, listing only `producing_version`. Accept it with one `evo tree authorize` (the default `rebase`), then repeat the same `evo tree evolve` command; no manifest edit is needed. In the release probes this held for `2.0.0b14` runs interrupted after `reflection_started`, `attempt_input_frozen`, `editor_started` and `round_closed`, with chat and subprocess roles on `/15` and `/16`, with a declared probe, with `on_unavailable`, with provider routing, with the b12 and b13 record fields, and for an unfinished run whose Attempts had closed `insufficient_evidence`: each finished `completed`, and `verify_evidence` and the audit found no mismatch. Runs written by `2.0.0b14` and `2.0.0b15` from the same manifest have identical identity and adaptation bytes. A `/16` run that `2.0.0b14` already finished after an `insufficient_evidence` Attempt stays finished: `evo tree evolve` refuses it with `run_identity_changed` and no effect, as for any finished run under another release, while `inspect_campaign(..., verify=True)` and `evo tree watch --once` read it under this release with no mismatch. The general rule still applies: finish or stop (Ctrl+C) every `/15` run before upgrading.

- **Rollback to `2.0.0b14` works for runs without `gate.stages`.** Such a `/15` or `/16` run resumes under `2.0.0b14` after one `rebase` listing `producing_version`, and finishes. A manifest that declares `gate.stages` is refused `unknown_key`. A run that declared `focus_first` cannot be read by `2.0.0b14` even after the key is removed, because its `campaign_created` policy, its `stage_opened` records and its `focus_decided` record carry the new fields: the resume ends with `persistence_failed` (`campaign_journal`, `journal_unavailable`) and exit `1`, and writes nothing into the run directory. Install `2.0.0b15` again and repeat the same command; the run continues without another authorization. Under `2.0.0b14` a run with an `insufficient_evidence` Attempt again fails at its end.


- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to refuse earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 stay refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b14] — 2026-10-27

### Added

- `/16` Reflector `probe.on_unavailable: suspend|continue`, with free unreachable calls, no continue-mode lockout and durable availability in reflection records and reports. The manifest reference documents the operator-owned probe launch contract.

- `/16` chat-role `provider_order` and `allow_fallbacks`, with normalized route verification for Editor and Reflector and private fallback telemetry.

- Root imports for target integrations: `EvalCase`, `Turn`, `parse_verdicts`, `is_artifact_path`, `PREFIX`, `STRATEGY`, `validate_instance_description` and `scan_tree`. Import these names from `evolution.tree` before later betas move their deep module paths under `evolution.tree.execution` and `evolution.tree.artifacts`. The target-server and agent guides now import the interaction values, the target server library and `ArtifactPolicyV1` from the root.

### Fixed

- A scorer trace writer could deadlock the campaign process when garbage collection finalized an old writer of the same pack while a new writer was registering its session on the same thread. The session registry now permits this reentrant release. Trace bytes, pack layout and session ownership rules are unchanged.

### Changed

- Shadow counts use distinct fields per operation, retaining the first oracle witness and counting repeated scans once. Reflector checks count every leaking sentence, and baseline trace paging shares field identities with compact evidence. Stored pre-b14 counts remain unchanged. `watch` shows hit codes and counts and probe health status.

### Upgrade

- New probe and routing keys are `/16` only and join identity only when declared. Adding, changing or removing one on an unfinished run needs one non-comparable `rebase` through `evo tree authorize`, accepting the exact reported findings, followed by the same evolve command. Undeclared keys preserve previous behavior, identities, journal bytes and report bytes. `/15` remains unchanged apart from the human-readable probe status in `watch`.

- **Upgrade to `2.0.0b14`.** Install the same release at both wire ends. An unfinished `/15` or `/16` run is refused once with `run_identity_changed`, effect `rebase`, listing only `producing_version`. Accept it with one `evo tree authorize` (the default `rebase`), then repeat the same `evo tree evolve` command; no manifest edit is needed. In the release probes this held for `2.0.0b13` runs interrupted after `reflection_started`, `attempt_input_frozen`, `editor_started` and `round_closed`, with chat and subprocess roles on `/15` and `/16`, with a declared probe, and for `/16` runs carrying `checks`, `shadowed`, `check_shadowed`, `leak`, `reflection_failed.detail` or `secret_oracle_paths`: each finished `completed`, and `verify_evidence` and the audit found no mismatch. Runs written by `2.0.0b13` and `2.0.0b14` from the same manifest have identical identity and adaptation bytes. The general rule still applies: finish or stop (Ctrl+C) every `/15` run before upgrading.

- **Rollback to `2.0.0b13` works for runs without the new keys.** Such a `/15` or `/16` run resumes under `2.0.0b13` after one `rebase` listing `producing_version`, and finishes. A manifest that declares `on_unavailable`, `provider_order` or `allow_fallbacks` is refused `unknown_key` at that key. A run that declared `on_unavailable` cannot be read by `2.0.0b13` even after the key is removed, because its Reflector identity and reflection records carry the new fields: the resume ends with `persistence_failed` (`campaign_journal`, `journal_unavailable`) and exit `1`, and writes nothing into the run directory. Install `2.0.0b14` again and repeat the same command; the run continues without another authorization. A run that declared only provider routing resumes under `2.0.0b13` once the keys are removed, after one `rebase` that also lists the changed chat role digests, and then runs without fallbacks.


- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to refuse earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 stay refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b13] — 2026-10-27

### Added

- `/16` policy check modes (`enforce`, `shadow`, `off`), metadata-only hit records and mode summaries in campaign reports and `watch`. Journal redaction and consistency guards remain mandatory.

- `/16` role-loop repair, tool-failure and fallback limits; indeterminate `min_tries` and `count_recovered`; and `target.preflight_sample_retries`. Omitted settings retain existing defaults and identity bytes.

- `/16` evidence settings with a full episode view, counted middle cuts, baseline `read_trace` access and inline observations cited by the diagnosis.

- A typed probe body and optional `/16` `probe.string_body: send|parse`; successful parsing is recorded only in the private probe document.

### Changed

- Pinned `/16` rounds use `edit_brief/6` (`/7` with attempt feedback), `reflect_brief/6`, `chat_tools/2` and `reflect_tools/3`. Custom grouping observation bytes include the full view. `/15` inputs and identities retain their existing bytes.

### Upgrade

- The first resume of an unfinished `/16` run requires one `rebase`. The open round keeps b12 inputs; later rounds use the new evidence settings.

- A subprocess Editor that accepts only `edit_brief/5` must also accept `edit_brief/7` under `/16`.

- **Upgrade to `2.0.0b13`.** Install the same release at both wire ends. An unfinished `/16` run is refused once with `run_identity_changed`, effect `rebase`, because every `/16` run now binds `evidence` in its adaptation identity. The refusal lists `adaptation` and `producing_version`, plus `identity.editor.digest` for a chat Editor and `identity.reflector.digest` for a chat Reflector. Accept all reported findings with one `evo tree authorize` (the default `rebase`), then repeat the same `evo tree evolve` command; no manifest edit is needed. An unfinished `/15` run is refused once with only `producing_version` and continues the same way. In the release probes this held for `2.0.0b12` runs interrupted after `reflection_started`, `attempt_input_frozen`, `editor_started` and `round_closed`, with chat and subprocess roles on `/15` and `/16`, and for `/16` runs carrying `leak`, `reflection_failed.detail` or `secret_oracle_paths`: an open Attempt crossed the version change and finished. The general rule still applies: finish or stop (Ctrl+C) every `/15` run before upgrading.

- **New `/16` settings are opt-in.** An absent `checks` key means every check enforces, and absent role, indeterminate and preflight keys keep their earlier values and identity bytes. Declaring or changing `checks`, `evidence` or `target.preflight_sample_retries` on resume is one more `rebase`; declaring or changing `min_tries` or `count_recovered` is a policy change that offers `rebaseline` or `close`. `2.0.0b12` refuses a manifest with any of these keys as `unknown_key`.

- **Rollback to `2.0.0b12` works for `/15` runs only.** A `/15` run that this release continued resumes under `2.0.0b12` after one `rebase` listing `producing_version`. `2.0.0b12` cannot read any `/16` run that this release created or continued, because its journal carries the adaptation `evidence`: its resume ends with `persistence_failed` (`campaign_journal`, `journal_unavailable`) and exit `1`. A manifest that declares `checks` is refused `unknown_key at checks` before that. `2.0.0b12` writes nothing into such a run directory. Install `2.0.0b13` again and repeat the same command; the run continues without another authorization. Plan to finish `/16` runs under this release.


- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to refuse earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 stay refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

### Fixed

- Nested run secrets are redacted longest first in role inputs and journal text, including `/15` inputs that previously exposed the longer secret's suffix.

- Family 15 verification now accepts a second consecutive full Grouper fallback when its bound limit requires a `grouper_failing` suspension, matching the driver and replay.

- **`evo tree conformance` accepts a `target.min_input_bytes` below the smallest turn body.** Such a value is valid in the manifest, but the `limits` check refused its own request with `declared input limit 1 is below the minimal turn body`. The check now sends the smallest turn body (86 bytes, empty content) in that case. A target that declares a `max_input_bytes` below that body still fails the check.

- **A `/16` gate no longer crashes after a recovered target failure.** A sample retry could score a try whose first Target turn had failed, while that failed turn still crossed the target instability threshold. A family-15 Attempt with `sequential` attempt feedback or a `first_accepted` round stop then entered the comparison without a finished gate measurement and stopped with an `AssertionError`. The campaign now suspends with `target_unstable` at the round barrier, as for any other crossed threshold; resume it with the same command after repair.

- **A flaky journal test no longer compares access times.** `test_create_durable_and_nonmutating_inspection` compared the whole `stat()` result, which includes `st_atime`, so a read on a `relatime` mount could fail it. It now compares the fields a mutation changes and the file bytes. Test only.

## [2.0.0b12] — 2026-10-25

### Added

- **Declare secret oracle subtrees in `/16`, ).** Optional `secret_oracle_paths` selects which oracle strings of at least eight characters are guarded. An absent key keeps the earlier set; `pointers: []` disables oracle checks, with the operator accepting that reference answers can reach the Editor. Run-secret checks stay enabled. A typo is refused `secret_oracle_path_unmatched` before writes. Adding, changing or removing the declaration takes a non-comparable `rebase`. Oracle refusal records can carry `leak` with the first case and oracle pointer; a secret-matching or oversized pointer is omitted. This evidence never reaches roles, and `watch` shows only the case id.

- **Normalize empty Reflector keys and name the failed field in `/16`, ).** Every Reflector kind drops unknown group keys with `null`, `""`, `[]` or `{}` values after screening the raw result for run secrets, before bounds and merging. Non-empty unknown keys remain defects; chat corrections name schema keys without quoting the model's key. `reflection_failed.detail` carries a closed category and constant path from final admission or the last semantic rejection before `turns_exhausted`. `watch` shows both without a rejected value.

- **Correct `/16` chat Editor plans at turn cost, ).** Create/update verbs follow the file system; successful writable mutations add unplanned files within `max_changed_files`. The latest accepted plan replaces its predecessors and keeps already changed files. Other plan mistakes return corrections in the same conversation and consume neither protocol repairs nor the consecutive-failure counter. Malformed calls keep the two-repair budget. The private log records normalizations and closed correction categories; `watch` prints only the last category and its log path. `/15` keeps its earlier rules and bytes.

- **A `/16` chat Reflector corrects a rejected answer in the same conversation, , ).** In `/16` with `reflector.kind: chat`, `finish_reflection` is now checked inside the role with the campaign's own admission rule. A fixable mistake, such as a `case_handle` in `affected_cases` or a case claimed through another case's observation, returns a message that names the field path and the problem, and the Reflector can revise and call `finish_reflection` again. The message never repeats the rejected value. Rejected answers stay in the private `reflector-logs/` transcript as `finish_reflection_rejected`; the journal records only the accepted answer, so no Attempt starts before it. Corrections use ordinary turns of the effective limit (`reflector.max_turns`, or `max(reflector.max_turns, 2 * probe.budget + 8)` with a probe) and no protocol repairs; there is no new manifest key. An answer with an oracle string or a run secret is never corrected and fails as before. `/15` is unchanged. Non-chat `/16` Reflectors still receive no in-role corrections; their empty-key normalization and raw secret screening follow the rules above.

### Changed

- **The `/16` chat Editor and Reflector have new identities.** The Editor adds the `plan_correction/1` identity flag, `{"record_plan": "plan_correction/1"}`. The Reflector sends the new packaged prompt `prompts/tree_reflector_v4.md` and a `finish_reflection` description that state the correction contract; its digest changes even with a declared `system_prompt`. Migration step: an unfinished `/16` run with a chat Editor is refused once with `run_identity_changed`, listing `producing_version` and `identity.editor.digest`; with a chat Reflector it also lists `identity.reflector.digest`. Accept all reported findings with one `evo tree authorize` and the default `rebase`, then repeat the same `evo tree evolve` command. A run with only a chat Reflector follows the same process for its reported fields. `/15` identities and `prompts/tree_reflector_v3.md` stay unchanged. A `2.0.0b11` reader cannot read a run carrying `leak`, `detail` or `identity.secret_oracle_paths`.

- **A `/16` chat Reflector's leak is reported first.** When its answer holds an oracle string or a run secret together with another defect, the reflection fails with the leak's code, `oracle_leak` or `invalid_result`, where `2.0.0b11` could report `invalid_result` for a length, key or claim defect found first. `/15` keeps its codes. For every `/16` Reflector kind, chat included, the raw run-secret screen now runs first (R7): a result carrying both a run secret and an oracle string fails `invalid_result` instead of `oracle_leak`.

- **A `/16` command Grouper's oracle refusal is recorded as `oracle_leak`.** Its output check used to report an oracle string and a run secret under one code, so `grouping_failed` carried `secret_detected` for both. In `/16` an output holding an oracle string now records `oracle_leak` with `leak`; a run secret alone still records `secret_detected`. `/15` keeps `secret_detected` for both.

- Storage modules under `evolution.tree` moved to `evolution.tree.storage`, step 2), with no behavior change. The envelope grammar moved out of `journal_schema`; `tree_inodes` moved to `artifact_policy`, beside `TreeScan`. `Envelope.from_payload` and `Envelope.from_bytes` now require the `family` keyword; a caller that relied on the old default passes `family=evolution.tree.journal_schema.S1_FAMILY`. `journal_schema` still imports the moved envelope names for its own use, so `evolution.tree.journal_schema:Envelope` and its neighbours still resolve; import them from `evolution.tree.storage.envelope` now, because the old path stops working when `journal_schema` moves. Update deep imports using the table below.

| Old Python name (`module:name`) | New Python name (`module:name`) |
|---|---|
| `evolution.tree.append_log:Snapshot` | `evolution.tree.storage.append_log:Snapshot` |
| `evolution.tree.append_log:LogState` | `evolution.tree.storage.append_log:LogState` |
| `evolution.tree.append_log:_fault` | `evolution.tree.storage.append_log:_fault` |
| `evolution.tree.append_log:_validate_file` | `evolution.tree.storage.append_log:_validate_file` |
| `evolution.tree.append_log:_directory` | `evolution.tree.storage.append_log:_directory` |
| `evolution.tree.append_log:_file` | `evolution.tree.storage.append_log:_file` |
| `evolution.tree.append_log:_read` | `evolution.tree.storage.append_log:_read` |
| `evolution.tree.append_log:_scan` | `evolution.tree.storage.append_log:_scan` |
| `evolution.tree.append_log:inspect_log` | `evolution.tree.storage.append_log:inspect_log` |
| `evolution.tree.append_log:AppendLog` | `evolution.tree.storage.append_log:AppendLog` |
| `evolution.tree.journal_errors:JournalCode` | `evolution.tree.storage.journal_errors:JournalCode` |
| `evolution.tree.journal_errors:JournalBoundaryError` | `evolution.tree.storage.journal_errors:JournalBoundaryError` |
| `evolution.tree.journal_errors:JournalFault` | `evolution.tree.storage.journal_errors:JournalFault` |
| `evolution.tree.journal_errors:JournalAdmissionRefused` | `evolution.tree.storage.journal_errors:JournalAdmissionRefused` |
| `evolution.tree.journal_errors:JournalRecoveryRequired` | `evolution.tree.storage.journal_errors:JournalRecoveryRequired` |
| `evolution.tree.state:_HIDDEN_REASONING` | `evolution.tree.storage.redaction:_HIDDEN_REASONING` |
| `evolution.tree.state:_SECRET_KEY` | `evolution.tree.storage.redaction:_SECRET_KEY` |
| `evolution.tree.state:safe_record` | `evolution.tree.storage.redaction:safe_record` |
| `evolution.tree.state:_safe` | `evolution.tree.storage.redaction:_safe` |
| `evolution.tree.state:_json_value` | `evolution.tree.storage.redaction:_json_value` |
| `evolution.tree.inode_floor:TRY_INODES` | `evolution.tree.storage.inode_floor:TRY_INODES` |
| `evolution.tree.inode_floor:MEASUREMENT_INODES` | `evolution.tree.storage.inode_floor:MEASUREMENT_INODES` |
| `evolution.tree.inode_floor:PREFLIGHT_INODES` | `evolution.tree.storage.inode_floor:PREFLIGHT_INODES` |
| `evolution.tree.inode_floor:ROLE_CONTROL_INODES` | `evolution.tree.storage.inode_floor:ROLE_CONTROL_INODES` |
| `evolution.tree.inode_floor:CAMPAIGN_FIXED_ENTRIES` | `evolution.tree.storage.inode_floor:CAMPAIGN_FIXED_ENTRIES` |
| `evolution.tree.inode_floor:EVALUATION_FIXED_ENTRIES` | `evolution.tree.storage.inode_floor:EVALUATION_FIXED_ENTRIES` |
| `evolution.tree.inode_floor:EVALUATION_MEASUREMENT_ENTRIES` | `evolution.tree.storage.inode_floor:EVALUATION_MEASUREMENT_ENTRIES` |
| `evolution.tree.inode_floor:InodeNeed` | `evolution.tree.storage.inode_floor:InodeNeed` |
| `evolution.tree.inode_floor:CampaignTerms` | `evolution.tree.storage.inode_floor:CampaignTerms` |
| `evolution.tree.inode_floor:tree_inodes` | `evolution.tree.artifact_policy:tree_inodes` |
| `evolution.tree.inode_floor:count_tree` | `evolution.tree.storage.inode_floor:count_tree` |
| `evolution.tree.inode_floor:shares_tmp` | `evolution.tree.storage.inode_floor:shares_tmp` |
| `evolution.tree.inode_floor:existing_ancestor` | `evolution.tree.storage.inode_floor:existing_ancestor` |
| `evolution.tree.inode_floor:measurements_per_round` | `evolution.tree.storage.inode_floor:measurements_per_round` |
| `evolution.tree.inode_floor:trace_inodes` | `evolution.tree.storage.inode_floor:trace_inodes` |
| `evolution.tree.inode_floor:other_entries_per_round` | `evolution.tree.storage.inode_floor:other_entries_per_round` |
| `evolution.tree.inode_floor:scorer_slots` | `evolution.tree.storage.inode_floor:scorer_slots` |
| `evolution.tree.inode_floor:round_need` | `evolution.tree.storage.inode_floor:round_need` |
| `evolution.tree.inode_floor:start_need` | `evolution.tree.storage.inode_floor:start_need` |
| `evolution.tree.inode_floor:campaign_need` | `evolution.tree.storage.inode_floor:campaign_need` |
| `evolution.tree.inode_floor:evaluation_need` | `evolution.tree.storage.inode_floor:evaluation_need` |
| `evolution.tree.inode_floor:evaluation_resume_need` | `evolution.tree.storage.inode_floor:evaluation_resume_need` |
| `evolution.tree.inode_floor:_low_floor_note` | `evolution.tree.storage.inode_floor:_low_floor_note` |
| `evolution.tree.inode_floor:whole_run_note` | `evolution.tree.storage.inode_floor:whole_run_note` |
| `evolution.tree.inode_floor:_missing` | `evolution.tree.storage.inode_floor:_missing` |
| `evolution.tree.journal_schema:IDENTIFIER` | `evolution.tree.storage.envelope:IDENTIFIER` |
| `evolution.tree.journal_schema:SchemaError` | `evolution.tree.storage.envelope:SchemaError` |
| `evolution.tree.journal_schema:_require` | `evolution.tree.storage.envelope:_require` |
| `evolution.tree.journal_schema:is_identifier` | `evolution.tree.storage.envelope:is_identifier` |
| `evolution.tree.journal_schema:_identifier` | `evolution.tree.storage.envelope:_identifier` |
| `evolution.tree.journal_schema:_integer` | `evolution.tree.storage.envelope:_integer` |
| `evolution.tree.journal_schema:_keys` | `evolution.tree.storage.envelope:_keys` |
| `evolution.tree.journal_schema:_pairs` | `evolution.tree.storage.envelope:_pairs` |
| `evolution.tree.journal_schema:_finite` | `evolution.tree.storage.envelope:_finite` |
| `evolution.tree.journal_schema:_canonical` | `evolution.tree.storage.envelope:_canonical` |
| `evolution.tree.journal_schema:EnvelopeFamily` | `evolution.tree.storage.envelope:EnvelopeFamily` |
| `evolution.tree.journal_schema:SecretMatch` | `evolution.tree.storage.envelope:SecretMatch` |
| `evolution.tree.journal_schema:secret_match` | `evolution.tree.storage.envelope:secret_match` |
| `evolution.tree.journal_schema:Envelope` | `evolution.tree.storage.envelope:Envelope` |
| `evolution.tree.journal_schema:LifecycleTransition` | `evolution.tree.storage.envelope:LifecycleTransition` |

### Upgrade

- **Upgrade to `2.0.0b12`.** Install the same release at both wire ends. An unfinished `/16` run with a chat Editor or a chat Reflector is refused once with `run_identity_changed`, effect `rebase`. The refusal lists `producing_version`, plus `identity.editor.digest` for a chat Editor and `identity.reflector.digest` for a chat Reflector. Accept all reported findings with one `evo tree authorize` (the default `rebase`), then repeat the same `evo tree evolve` command; no manifest edit is needed. An unfinished `/15` run, and a `/16` run without a chat Editor or chat Reflector, is refused once with only `producing_version` and continues the same way (probed with chat and subprocess roles on `/15` and subprocess roles on `/16`). In the release probes this held for runs interrupted after `reflection_started`, `attempt_input_frozen`, `editor_started` and `round_closed`: an open Attempt crossed the version change and finished, with no `frozen editor common input changed` stop. The general rule still applies: finish or stop (Ctrl+C) every `/15` run before upgrading.

- **Secret oracle paths are opt-in.** An existing `/16` run keeps its oracle needle set until its manifest declares `secret_oracle_paths`; adding the key takes the non-comparable `rebase` described under Added. `2.0.0b11` refuses a manifest with the key as `unknown_key at secret_oracle_paths`.

- **Rollback to `2.0.0b11` is limited.** A run that this release continued but that carries none of the new fields resumes under `2.0.0b11` after one `rebase` listing the same fields as above (`producing_version`, and the chat role digests where they apply). A run whose journal carries `leak` (an oracle refusal in `/16`), `reflection_failed.detail` (a `/16` Reflector failure from admission) or `identity.secret_oracle_paths` cannot be read by `2.0.0b11`: its resume ends with `persistence_failed` (`campaign_journal`, `journal_unavailable`) and exit `1`, and a manifest that still declares `secret_oracle_paths` is refused `unknown_key` first. `2.0.0b11` writes nothing into such a run directory. Install `2.0.0b12` again and repeat the same command; the run continues without another authorization. Any `/16` run continued under this release gains `leak` when an oracle check refuses a Reflector, Grouper or seal result, and `detail` when a Reflector result fails admission, so plan to finish `/16` runs under this release. Rolling back also removes the in-role corrections: a `/16` chat Reflector's fixable mistake again fails the reflection, and a chat Editor's plan mistake again ends the Editor.


- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to refuse earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 stay refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b11] — 2026-10-23

### Fixed

- **Scorer run secrets no longer reach role inputs.** A scorer's forwarded `allowed_env` values are run secrets, but a scorer that printed one into a verdict note, `public_feedback` or `diagnostics` passed it to the Reflector and the Grouper, and a bare token in `public_feedback` reached the Editor's brief; a `chat` or `agent_cli` role then sent it to its model provider. Now every run secret in those fields is replaced by `[REDACTED]` before the Reflector or the Grouper reads them, the Editor's grammar drops and counts a `public_feedback` entry that holds one, and a criterion id that holds one makes that output unavailable to the roles. No secret is written to the journal, and scorer traces are unchanged; output without a run secret gives the same role inputs as before. Where a scorer printed a run secret, the digest of an Attempt's frozen Editor input differs from `2.0.0b10` (see Upgrade).

- **A `/16` round whose reflection gives the Editor nothing to do closes again, `severity:critical`).** In family 15 (`/16`), when the Reflector's settled result was no valid Editor input, for example `reflection_failed` with `invalid_result` after a diagnosis that cited unknown evidence, or a declined reflection, the round's `round_closed(unplanned)` record was refused before it was written: the campaign stopped with `CampaignInvariantViolated: invalid journal record` and the round stayed open. Every `round_closed` record now carries `attempts_opened`, as the journal schema of families 14 and 15 already required. Such a round closes `unplanned` with no Attempt, no Candidate measurement and the Champion kept, and the campaign goes on to its next round. Migration step: a run that stopped this way has a journal that ends at `reflection_failed`; run the same `evo tree evolve` command with the same run directory again, and the round closes and the campaign continues. Family 14 (`/15`) runs are unchanged, and no record shape, digest or exit code changes.

### Upgrade

- **Upgrade to `2.0.0b11`.** Finish or stop (Ctrl+C) every `/15` run before upgrading, install the same release at both wire ends, accept the reported producing-version `rebase` through `evo tree authorize`, then repeat the same `evolve` command. A `/16` run continues the same way, with one exception below. A `/16` run that `2.0.0b10` stopped with `CampaignInvariantViolated: invalid journal record` after `reflection_failed` takes the same steps: its first resume under this release is refused once with `run_identity_changed`, listing only `producing_version`; after that one `rebase` the same command closes the round `unplanned` and the campaign continues. No manifest edit is needed. A scorer whose output holds no run secret gives the roles the same inputs as under `2.0.0b10`.

- **Exception: an open Attempt over scorer output that holds a run secret cannot cross the version change.** When a scorer printed a forwarded `allowed_env` value into `public_feedback`, the Editor's brief differs between `2.0.0b10` and this release, because this release drops that entry. An Attempt records the digest of its brief in `attempt_input_frozen` and checks it before the Editor starts. A `/15` or `/16` run that was suspended or crashed after `attempt_input_frozen` and before `editor_finished` therefore stops, after the accepted producing-version `rebase`, with `CampaignInvariantViolated: frozen editor common input changed`, and repeating the command stops the same way. Go back to the release that froze the Attempt, accept one more producing-version `rebase` and run the same command; that Attempt and the rest of the run then finish there. The journal is not damaged. To avoid this, finish or stop such a `/16` run before upgrading too. A run interrupted at another point, an interrupted grouping or reflection included, resumes after the one `rebase`.

- **The upgrade can be rolled back, with the same exception.** This release adds no record shape that `2.0.0b10` cannot read. The `attempts_opened` field that #318 adds to family-15 `round_closed` records was already required by the `2.0.0b10` journal schema. #313 changes what the roles read and, for a scorer that printed a run secret, the digest that binds the Editor's frozen input (`attempt_input_frozen`). It does not change the scorer traces, and it writes no secret into the journal. A run continued by this release resumes under `2.0.0b10` after the same single producing-version `rebase`, except an Attempt as above, which must finish under this release first. Rolling back brings both defects back: scorer run secrets reach the roles again, and a `/16` round whose reflection is no valid Editor input stops the campaign again.


- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to refuse earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 stay refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b10] — 2026-10-21

### Added

- **Plain HTTP to a model endpoint on any host.** The `base_url` of a `chat` Editor, Reflector or Grouper in `/15` and `/16` accepts `http://` to any host, for example `http://llm.internal:8000/v1` inside a closed network; it was limited to `127.0.0.1`, `localhost` and `::1`. Every other rule stays: a valid host, a port that parses, and no userinfo, query, fragment, whitespace or control character. For each such role, `evo tree evolve` prints one line on stderr before any role runs, on every invocation, and the process log keeps it: `evo tree evolve: WARNING: <role> sends its credential unencrypted to <base_url>` when the role declares `api_key_env`, `evo tree evolve: WARNING: <role> talks to <base_url> without encryption` when it does not. Nothing waits for input. On a terminal the `WARNING:` label is highlighted, unless the `NO_COLOR` environment variable is set to a non-empty value. `evolution.tree.evolve.unencrypted_model_roles(workflow)` returns the same roles. HTTPS and loopback HTTP print nothing new, and a document that was valid before keeps its identity and policy digests. No status or report caveat is added. `2.0.0b9` refuses such a document with `bad_value at <role>.base_url`.

- **Chat roles without credentials.** In `/15` and `/16`, a `chat` Editor, Reflector or Grouper may omit `api_key_env` or declare it `null`; it then calls its endpoint without credentials, and you no longer export a dummy key for an endpoint that needs none. Its requests carry no `Authorization`, `api-key`, `OpenAI-Organization` or `OpenAI-Project` header, and its client reads no credential from the process environment: `OPENAI_API_KEY`, `OPENAI_ADMIN_KEY`, `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID` are ignored for that role. A header that `OPENAI_CUSTOM_HEADERS` names is removed from its requests, except protocol headers such as `Content-Type`. The headers are removed at the HTTP transport, below the SDK. The tests that capture the request headers ran on `openai` 2.53.0 (the lock) and 3.8.0 (the development environment); another `openai` version is outside what they show. Before any role runs, `evo tree evolve` prints `evo tree evolve: <role> calls <base_url> without credentials (no api_key_env declared)` once per such role, on every invocation, and the process log keeps it; `evolution.tree.evolve.unauthenticated_roles(workflow)` returns the same roles. A declared `api_key_env` keeps its behavior exactly: a missing or empty variable is still refused (`editor_env_missing`, `reflector_env_missing`, `grouper_env_missing`, exit `2`), the role sends its own key, and a document that declares it keeps its identities, digests, journal records and report bytes. A `401` or `403` stays `credential` / `provider_rejected`. An undeclared key is left out of the role's identity. Migration step: adding or removing `api_key_env` on a running campaign is refused once as `run_identity_changed`; accept it with one `evo tree authorize` (a `rebase`) and run the same command again. `2.0.0b9` refuses a document without `api_key_env` for a chat role (`missing_key`), so it can neither start nor resume such a campaign. It also cannot read a run whose Grouper identity omits the key (the adaptation-identity validator requires it). A run whose only unauthenticated roles are the Editor or the Reflector stays readable by `2.0.0b9`, because their persisted identities are opaque digests. The short-value note `forwarded value <NAME> is shorter than 8 characters and is not redacted` now also covers the Reflector's names.

- **Visible provider failures.** Every `provider_attempt` record in a chat Editor's, Reflector's or Grouper's private role log gains `http_status` (the integer status of a failed attempt, else `null`) and `provider_code` (the provider's `error.code` or `error.type` when it matches `[A-Za-z0-9_.:-]{1,64}`, else `null`), the tool-dialect preflight's and the attempts a retry recovered included; the preflight's and the Editor's `model_request_failed` gain the same two fields. No provider message text is recorded. When one role invocation (an Editor Attempt, a reflection or a grouping operation) had failed provider attempts, `evo tree evolve` prints one line on stderr, kept by the process log: `evo tree evolve: reflector r01 needed provider retries (2 failed attempts: http 503, http 503); it continued` when it went on, with `(tool-dialect preflight)` when all of them were the preflight's, or, after the failure report of an invocation that ended in a provider failure, `evo tree evolve: reflector r01 provider attempts: http 503, http 503, http 503 (provider_unavailable)`. The line lists at most eight statuses, then `…`; a `401` or `403` reads `http 401 (credential)`, and a role without `api_key_env` adds `the endpoint asked for a credential; this role declares no api_key_env`. The reason in parentheses is the retry layer's (`provider_unavailable`, `deadline_exhausted` or `provider_rejected`); the journal still records the first two as `provider_exhausted`. A dialect rejection that the preflight answers by trying the next tool format prints no line and stays in the role log. The journal, the stdout document, the report, the retry policy and every failure reason are unchanged.

- Public `evolution.tree.conformance_manifest(document, *, base)` builds workflow-less service input from exactly `seed_root`, `target` and `artifacts`, using the shared strict validation for conformance, artifact-store and HTTP-adapter inputs.

- Optional `/16` gate choices for noisy agents and small scopes: `gate.mode: statistical` uses the family-14 repeat bootstrap in one fixed stage at R>=2; `causal.tolerance: "measured_noise"` calibrates from Parent rows only in point, statistical and confidence modes at R>=2, with no extra measurement; `gate.bound_fallback: "point"` labels a weaker per-scope point decision when the confidence bound is structurally unattainable. Reports expose bootstrap exactness and pairing, empirical caveats, calibration source and fallback reasons. Small-R exact contrasts decide on the point; conservative confidence width depends on units, not R. Without these gate options, `/15` and `/16` keep their policy digests and gate behavior. `2.0.0b9` and earlier reject the new options and cannot read runs that use them. See [the gate reference](docs/manifest-v2.md#independent-controls-manifest-16) and [operator guidance](docs/running-campaigns.md#choose-independent-acceptance-manifest-16).

- **A configurable Reflector group text limit in `/15` and `/16`, ).** A long but valid diagnosis no longer has to cost the round. The optional `reflector.group_text_chars` sets the most characters each of the five group text fields may hold: an integer of at least `1`, or `null` for no per-field limit. It counts characters, not tokens. Absent, it is 512 in `/15`, which keeps every byte of a `/15` document without the key, and 4,096 in `/16`. The chat tool schema, the `chat`, `agent_cli` and `subprocess` checks, both briefs and the journal's check of stored groups all use the same limit. The 65,536-byte cap on all groups stays, so about three groups of five full 4,096-character fields fit. Each round keeps the limit in force when it opened; a change accepted through `evo tree authorize` applies from the next round, and a lowered limit never invalidates stored groups. Every layer now also checks the groups as the journal stores them: a repeated evidence ID counts each time it is listed, so a `chat` answer that used to pass the tool and then stop the campaign at the journal append now gets #180's repair, an `agent_cli` answer gets its single validation repair, and a `subprocess` answer fails `invalid_result`. See [the group text limit](docs/manifest-v2.md#the-reflector-group-text-limit-15-and-16).

### Breaking

- **A run secret in the instance description is refused.** The connection preflight of `/15` and `/16` tests the probe instance's `description` with the journal's own secret predicate, before the rollout. A match ends the probe as `connection_incompatible` with the new detail code `connection_description_secret`; the campaign suspends with owner `connection`, exit `1`. A later instance is checked when it is created, before any row runs on it; a match ends the invocation as the resumable result `persistence_failed` with owner `campaign_journal` and code `record_matches_secret`, and no `instance_attested` record is written for that instance (the measurement journal still records its creation and release). Migration step: none for a target whose descriptions hold no forwarded value of eight or more characters. Otherwise remove the value from `describe()` (for example report a hash of the model id), or stop forwarding it, and run the same command again. A journal holding the new detail code cannot be read by `2.0.0b9`. To stop forwarding a name, note that removing it from `allowed_env` changes the Scorer's identity and needs `evo tree authorize`.

- **Campaign families 14 and 15 only.** Commands and library readers now refuse family 3–13 run directories and unversioned creation records with `campaign_family_unsupported at family/N` (`absent` for an unversioned record); CLI refusals exit `2`. This includes inspection, watch, trace, evaluation from a campaign run directory, and authorization with or without `--list`. Every campaign manifest loader and command, including `evo tree conformance`, `evo-target serve` and `evo tree trace --manifest`, refuses `/1`–`/14` with `manifest_schema`. New links to families 3–13 are refused with `lineage_family_unsupported`: `/15` links family 14; `/16` links 14 or 15. Migration: read old run directories with `2.0.0b9`; seed a new `/15` campaign from the Champion copy. There is no converter or replacement alias. Existing family-14/15 runs with recorded family-13 predecessors remain readable; an owed baseline import reports `BaselineImportUnavailable` and requires the existing `fresh_baseline` authorization. Standalone evaluation manifests are unchanged.

| Python name (`module:name`) | Result |
|---|---|
| `evolution.tree:SCHEMA_V5` | removed |
| `evolution.tree:SCHEMA_V6` | removed |
| `evolution.tree:SCHEMA_V7` | removed |
| `evolution.tree:SCHEMA_V8` | removed |
| `evolution.tree:SCHEMA_V9` | removed |
| `evolution.tree:SCHEMA_V10` | removed |
| `evolution.tree:SCHEMA_V11` | removed |
| `evolution.tree:SCHEMA_V12` | removed |
| `evolution.tree:SCHEMA_V13` | removed |
| `evolution.tree:SCHEMA_V14` | removed |
| `evolution.tree.manifest_v2:SCHEMA` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V1` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V2` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V3` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V4` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V5` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V6` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V7` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V8` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V9` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V10` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V11` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V12` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V13` | removed |
| `evolution.tree.manifest_v2:SCHEMA_V14` | removed |
| `evolution.tree:CampaignPolicyV1` | removed |
| `evolution.tree:CampaignPolicyV5` | removed |
| `evolution.tree:CampaignPolicyV7` | removed |
| `evolution.tree:CampaignPolicyV8` | removed |
| `evolution.tree:CampaignPolicyV10` | removed |
| `evolution.tree:CONTINUABLE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree:SUSPENDABLE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree:DIAGNOSTIC_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree:MODEL_GROUPING_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree:RESILIENCE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree:CONFIDENCE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_policy:CampaignPolicyV1` | removed |
| `evolution.tree.campaign_policy:CampaignPolicyV4` | removed |
| `evolution.tree.campaign_policy:CampaignPolicyV5` | removed |
| `evolution.tree.campaign_policy:CampaignPolicyV7` | removed |
| `evolution.tree.campaign_policy:CampaignPolicyV8` | removed |
| `evolution.tree.campaign_policy:CampaignPolicyV9` | removed |
| `evolution.tree.campaign_policy:CampaignPolicyV10` | removed |
| `evolution.tree.campaign_schema:CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:ADAPTIVE_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:ADAPTIVE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:LAYERED_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:LAYERED_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:CONTINUABLE_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:CONTINUABLE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:SUSPENDABLE_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:SUSPENDABLE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:DIAGNOSTIC_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:DIAGNOSTIC_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:MODEL_GROUPING_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:MODEL_GROUPING_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:RESILIENCE_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:RESILIENCE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:SPLIT_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:SPLIT_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:AFFECTED_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:AFFECTED_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:CONFIDENCE_CAMPAIGN_FAMILY` | removed |
| `evolution.tree.campaign_schema:CONFIDENCE_CAMPAIGN_FAMILY_VERSION` | removed |
| `evolution.tree.campaign_schema:RECORDS` | removed |
| `evolution.tree.campaign_schema:validate_adaptive_payload` | removed |
| `evolution.tree.affected_gate:AffectedGateClosed` | removed |
| `evolution.tree.affected_gate:AffectedGateResult` | removed |
| `evolution.tree.affected_gate:affected_gate_at_look` | removed |
| `evolution.tree.affected_gate:affected_look_missing` | removed |
| `evolution.tree.campaign_gate:IntervalBounds` | removed |
| `evolution.tree.campaign_gate:LayeredDecision` | removed |
| `evolution.tree.campaign_gate:LayeredGateResult` | removed |
| `evolution.tree.campaign_gate:PublishedBound` | removed |
| `evolution.tree.campaign_gate:PublishedInterval` | removed |
| `evolution.tree.campaign_gate:below_regression_margin` | removed |
| `evolution.tree.campaign_gate:decide_affected_intervals` | removed |
| `evolution.tree.campaign_gate:decide_layered_intervals` | removed |
| `evolution.tree.campaign_gate:layered_gate_at_look` | removed |
| `evolution.tree.affected_report:CompletionInputs` | removed |
| `evolution.tree.affected_report:completion_caveats` | removed |
| `evolution.tree.affected_report:completion_items` | removed |
| `evolution.tree.affected_report:read_completion_inputs` | removed |
| `evolution.tree.affected_report:read_completion_items` | removed |
| `evolution.tree.campaign_identity:cohort_measurement_id_for` | removed |
| `evolution.tree.campaign_identity:completion_measurement_id_for` | removed |
| `evolution.tree.campaign_statistics:select` | removed |
| `evolution.tree.campaign_view:BindCompletion` | removed |
| `evolution.tree.campaign_view:CompleteWinner` | removed |
| `evolution.tree.campaign_view:CompleteWinnerStage` | removed |
| `evolution.prompts:TREE_AGENT_REFLECTOR_SYSTEM_V2` | removed |
| `evolution.prompts:TREE_REFLECTOR_SYSTEM_V2` | removed |

### Fixed

- Fixed #278: a stored grouping result refused for its shape or policy now reports `the stored grouping result is invalid (<code>)` and no longer tells the operator to restore scorer traces; a refusal that depends on the baseline registry still names them, with the code. A repair of the environment-documentation snapshot now waits for a deferred or held rebase and follows the round boundary's records, so the live documentation is never re-taken before the record that names its digest. No journal record, digest or exit code changes.

### Upgrade

- **Upgrade to `2.0.0b10`.** Finish or stop (Ctrl+C) every `/15` run before upgrading, install the same release at both wire ends, accept the reported producing-version `rebase` through `evo tree authorize`, then repeat the same `evolve` command. A `/16` run continues the same way; for a `/16` run created by `2.0.0b9`, that one `rebase` also accepts the 4,096-character Reflector limit (see above). A target whose instance description carries a forwarded secret now suspends the campaign. Read family 3–13 run directories with `2.0.0b9`.

- **Upgrade step for `/16` runs from `2.0.0b9`; a `/16` document without `group_text_chars` now means 4,096, not 512.** An unfinished `/16` run created by `2.0.0b9` or earlier ran at 512. Its first resume is refused once with `run_identity_changed`, listing `producing_version` and `adaptation`; one `evo tree authorize` accepts both, and the next round uses 4,096. To keep 512, declare `"group_text_chars": 512` in its `reflector` section before you resume, and accept the same single `rebase`. `/15` runs need only the usual producing-version `rebase`. `2.0.0b9` and earlier refuse a document that declares the key and cannot read a run whose identity carries it, which every `/16` run of this release does.

- **The upgrade cannot be rolled back.** After the `2.0.0b10` rebase, `2.0.0b9` cannot read a `/16` run (its identity carries `group_text_chars`) or a journal that holds the #287 detail code `connection_description_secret`. Keep a copy of the run directory if you may need `2.0.0b9`.

- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` now refuses earlier campaign manifests, including `evolution-v2-manifest/5`, with `manifest_schema`, and run directories of families 3–13 are refused `campaign_family_unsupported`. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b9] — 2026-10-20

- Changed #273: family-15 Attempt projections publish `gate_comparison` and `global_comparison` separately; `comparison` remains the global layer when present, otherwise the gate layer. Each comparison gains a nullable `reference_measurement_id`; `gate_comparison` and `global_comparison` are `null` when that layer is absent. The family-15 projection is closed: result and status documents stored by `2.0.0b8` are refused by this release and must be regenerated; a `2.0.0b8` reader refuses documents of this release.

- Fixed #268: a held family-15 `close` that includes an artifact-policy or seed-tree change now completes the owed promotion under the recorded store identity and policy, then closes with every accepted difference recorded and no store rebase.

- Fixed #269: family-15 authorization consequences and caller cancellation now wait for an already-selected round's promotion and closure. Accepted hygiene and other consequences precede the held condition decision, then cancellation; a held `close` keeps its own final terminal.

- Fixed #262: resuming an interrupted Attempt without its baseline measurement journal or scorer traces now returns `persistence_failed` (`measurement_journal`, `journal_unavailable`) instead of a traceback. The detail names the evidence to restore from a trusted copy; restoring it lets the same run resume.

- Fixed #272 (critical): a program declared with a symlinked virtual-environment interpreter now runs inside that environment; retargeting the link after bind is detected as a changed command. An interpreter started from a directory inside the seed, such as a seed virtual environment whose `python` links outside, is refused as `program_inside_seed`. This breaks a `/15` document that `2.0.0b8` admitted, under the beta rule for critical issues. Migration step: declare an interpreter outside the seed. An unfinished run with such a declaration cannot resume under this release; finish or stop it under `2.0.0b8`, then continue in a linked run whose manifest declares an interpreter outside the seed.

- **A lost artifact ledger is refused instead of deleting the run's artifacts, critical).** Before, a resume or reopen whose `artifacts.jsonl` was missing or empty treated the store as never created. It deleted `artifacts/` and recreated `v0`: a finished run with writable copies lost its final Champion copy and still exited `0`, and with read-only copies the command ended in a `PermissionError` traceback. Now, a resume or reopen that opens the artifact store refuses `artifact_ledger_missing` (hard, exit `2`) before any write when the campaign journal records measurements, leaving `artifacts/` as it is. An immediate authorized `close` opens no store, writes its terminal and deletes nothing. Fixed #270: the same refusal protects an evaluation whose measurement is bound; the campaign removal guard also holds under Python `-O`. A crash before the store's first record still recreates it, including when its ledger is empty. Migration step: restore `artifacts.jsonl` from a trusted copy of the run directory, then repeat the command. Scripts that match refusal codes gain one code; library callers see it as `evolution.tree.ArtifactLedgerMissing`.

- **Family 15: an authorized `close` or `continue` completes an owed promotion first.** When a crash left a durable selection without its round closure, the decision used to be refused. It now waits until that round's promotion and `round_closed` are written in the old condition, as `rebaseline` already did. Families 14 and earlier are unchanged. Migration step: none. The `rebaseline` workaround is no longer needed.

- **Upgrade to `2.0.0b9`.** Finish or stop (Ctrl+C) every `/15` run before upgrading, install the same release at both wire ends, accept the reported producing-version `rebase` through `evo tree authorize`, then repeat the same `evolve` command. A `/16` run continues the same way. Regenerate stored family-15 result and status documents with this release, and upgrade every reader of them at the same time. A program started from a directory inside the seed, even an interpreter whose `python` links outside, is now refused `program_inside_seed`.


- `evo tree evolve` admits `/15` and opt-in `/16`, and the `evo-target` scaffold continues to write `/15`. `evo tree conformance` continues to read earlier manifests, including `evolution-v2-manifest/5`, and run directories of every released family stay readable. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b8] — 2026-10-19

- **Choose measurement and acceptance independently.** Opt-in `evolution-v2-manifest/16` creates family 15 with R>=1, point/confidence/unfiltered acceptance, an explicit execution-noise allowance and reuse/fresh verification. Reuse measures only missing Candidate slots; every eligible Candidate has complete required coverage. A nonpassing numerical comparison continues within the declared budget; it does not pause for statistics or buy extra repeats. The `evo-target` scaffold continues to write `/15`, whose meaning is unchanged. See [independent acceptance](docs/running-campaigns.md#choose-independent-acceptance-manifest-16).

- **Promotion is automatic.** An eligible, fully measured Candidate is selected and published without an operator step. `/16` has no `campaign.promotion` key: a document that carries it is refused `unknown_key` at `campaign.promotion`, before a run directory is created. `evo tree evolve` and `evo tree watch` keep their existing exit codes. See and .

- **Interpret confidence for the frozen scope.** Confidence bounds concern the expected contrast for the frozen case or corpus-group scope under independent bounded outcomes. They establish neither task-population generalization nor a campaign-wide false-promotion rate. Declared noise allowance is reserved once; observed repeat variation is diagnostic and triggers no automatic measurement.

- **Carry bounded linked diagnostics.** A `/16` successor can link a finished family-13, family-14 or family-15 run and import compatible baseline diagnostics into a bounded private bundle. Missing or incompatible diagnostics produce explicit gaps without hidden baseline measurements. Captured historical stdout is hashed at import; this does not authenticate its historical content. Train/test and Editor privacy boundaries remain in force.

- **Rebaseline over an open `/16` round.** An authorized `rebaseline` closes the open round completely. A pre-release defect, fixed before this release, failed the family-15 journal schema after the authorization was durable and left the run stuck.

- **Upgrade to `2.0.0b8`.** Finish or stop (Ctrl+C) every `/15` run before upgrading, install the same release at both wire ends, accept the reported producing-version `rebase` through `evo tree authorize`, then repeat the same `evolve` command. `/16` requires a new manifest and run directory; do not convert an existing run or hand-edit its journals. Existing `/15` semantics and historical evidence interpretation are retained. A family-15 run directory made with a development build from before that correction cannot be read by this release; start a new run.

- **Known gap.** On family 15, an authorized `close` or `continue` is refused when a crash left a durable selection without its round closure. The journal stays intact. Authorize `rebaseline` instead, which completes the owed promotion first, or revert the target, resume to finish the round, then make the change. See the authorization note.


- `evo tree evolve` admits `/15` and opt-in `/16`. `evo tree conformance` continues to read earlier manifests, including `evolution-v2-manifest/5`, and run directories of every released family stay readable. `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b7] — 2026-10-17

- **Packed scorer traces.** New campaign traces use `scorer-traces/<measurement_id>.pack`, evaluations use `scorer-traces/evaluation.pack`, and connection preflights use `connection-logs/<operation_id>/<operation_id>.pack`. Path-reading tools must use the [public reader](docs/run-storage-layout.md#the-v2-run-directory); old traces stay in place and remain readable alongside packs. Requests are durable before spawn, and complete output before a scoring terminal. An Evolution crash during scoring loses partial in-flight output. Corrupt segments remain intact; restore evidence needed for resume, then allow later-segment continuation where permitted, or start a new campaign. Deleting evidence is not a repair.

- **Inode forecasts warn and continue.** Scripts must no longer rely on forecast-only `storage_inodes_insufficient`, a campaign capacity terminal or evaluation exit `2` as a pre-measurement stop. Surface the immediate stderr note, including with `--progress`, and handle actual storage faults. Warning prose is not a machine API; no structured warning/result field is added. Missing or ordinarily raising inode-note sinks cannot block work. Historical inode-capacity records remain readable. Byte refusals, unsafe-entry/identity checks, privacy and measurement gates are unchanged. Exhaustion can still fail Evolution or external programs under their existing classifications, after paid calls; recovery may repeat work.

- **Upgrade to `2.0.0b7`.** Finish or stop (Ctrl+C) every `/15` run before upgrading, install the same release at both wire ends, accept the reported producing-version `rebase` with `evo tree authorize`, then repeat the same `evolve` command. This release changes no role identity, prompt, Editor brief, frozen Attempt input, artifact matcher or `role_projection/1` digest. Old directory traces stay in place; moving a run back across packed writing is unsupported.

- **Known library cleanup limitation.** A long-lived library or in-process caller that abandons a cyclic scorer can deadlock if its finalizer re-enters the writer registry lock during writer initialization or close. Product-owned CLI scorers close explicitly. This issue is not fixed in this release.

- `evo tree evolve` still admits only `/15`. `evo tree conformance` still reads every manifest it read in `2.0.0b6`, including `evolution-v2-manifest/5`, and run directories of every family stay readable. The `evo-target` scaffold still writes `/15`; `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b6] — 2026-10-17

- **Bounded scorer-trace reads and strict run boundaries.** `evolution.tree.ScorerTraceReader.read` and `read_scorer_trace` gain `max_section_bytes=None`, an inclusive byte cap on each requested section. A finite cap raises `ScorerTraceUnreadable` with code `too_large` without returning a partial record; it applies to directory and pack payloads. `ScorerTraceReader` and `read_scorer_trace` gain `run_boundary="resolved"`, with opt-in `"nofollow"` traversal of the supplied path and its ancestors. Defaults preserve unlimited reads and resolved paths. Existing imports, calls, absence semantics and both layouts remain supported. See the [32 MiB example and API limits](docs/run-storage-layout.md#the-v2-run-directory).

- **Strict reader lifecycle.** An unsafe, absent or unreadable strict run root fails during construction with `unsafe_entry`; catch `ScorerTraceUnreadable` around construction as well as reads. Strict readers pin the run directory until terminal, idempotent `close()`; later reads or context entry raise `ValueError`. Construct a new reader for a new boundary or later pack observations: pack indexes are cached on first use. Directory traces remain uncached. The section cap is not a total pack-index or process-RSS memory cap.

- `evo tree evolve` still admits only `/15`. `evo tree conformance` still reads every manifest it read in `2.0.0b5`, including `evolution-v2-manifest/5`, and run directories of every family stay readable. The `evo-target` scaffold still writes `/15`; `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b5] — 2026-10-16

- **Read scorer traces through one function.** `evolution.tree.read_scorer_trace` and `ScorerTraceReader` return one try's request, stdout and stderr as a `ScorerTraceRecord`, with the byte size of each section; an unreadable trace raises `ScorerTraceUnreadable` with a closed `code`. They read campaign and evaluation traces by measurement id and connection-preflight traces by operation id. These four names are a promised surface of the 2.x beta.

- **Break for tools that open trace files by path.** An empty `stdout` or `stderr` file is no longer kept, so a missing stream file next to `request.json` now means an empty stream, not only an interrupted try. Migration step: read traces with `evolution.tree.read_scorer_trace`, starting with this release. The next beta writes new traces into one file per measurement, `scorer-traces/<measurement_id>.pack`; the reader reads both layouts, and traces of older runs are never moved.

- **Free inodes are checked before measuring.** A `/15` campaign whose filesystem cannot hold one round by Evolution's estimate stops at its start or resume with `storage_capacity_exhausted` and code `storage_inodes_insufficient`, and stderr states free and required inodes. An evaluation is refused with `refused: storage_inodes_insufficient` (exit `2`). The estimate is a floor, not a guarantee. A filesystem without an inode limit is not checked. A campaign that may not hold all its rounds starts with a note.

- **A failed scorer-trace write is a storage fault.** When the disk or its inodes run out while a scorer trace is written, the campaign ends in the resumable `storage_capacity_exhausted` terminal (another storage error: `persistence_failed`) instead of recording a scorer failure, and one stderr line names the trace file. An evaluation stops with `journal unavailable` (exit `1`) and the same line; run the same command again after the repair.

- **Library callers of `durable_score`.** A journal `run_id` outside `[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}` is refused with `ValueError` when the scorer offers `score_traced`, before anything is journaled. Every id Evolution writes matches.

- **Journal readers.** A campaign journal's `capacity_exhausted` record can carry the code `storage_inodes_insufficient`.

- **`check_corpus_placement` checks declared paths only, and `run_root` is optional.** It no longer looks up an `agent_cli` role without `agent_bin` on `PATH`, so it never reports `editor_program_missing`; `run_root=None` skips the run-directory check; the `environ` argument is accepted and ignored. `evo tree evolve` is unchanged and also refuses a corpus that is the agent CLI found on `PATH`.

- **Bounded Reflector history for every kind.** Family 14 (`/15`) uses `reflect_brief/5` with `reflect_history/1`: recent detail and summary rounds plus a bounded whole-run `reflect_history_aggregate/1`. Distinct changes to visible paths have `reflect_change_fingerprint/1` digests, observed outcomes and pointers, without causal claims. Full Attempt results, edit excerpts, diagnoses and per-Attempt change records (`changes:<attempt_id>`, `reflect_attempt_changes/1`) stay readable with `list_history` and `read_history` through native chat tools and the `agent_cli`/`subprocess` launchers. Custom `subprocess` Reflectors that parse `evidence.history` must handle this shape. History is at most 96,000 serialized characters; the Editor's frozen brief and history are unchanged.

- **A chat Reflector refuses an oversized first request locally.** Its compaction keys also set how much opening history is shown and its first-request budget. A fully elided request above the budget fails `input_too_large` before probe checks or any provider call, including preflight, and suspends `reflector_failing` without closing the round. Raise `reflector.context_compact_chars`, accept the printed identity `rebase` with `evo tree authorize`, then run the same `evo tree evolve` command again; or shrink the fixed parts, such as environment documents or the system prompt. A run whose fixed parts alone are too large could work before b5 and learns of this refusal at its next reflection. See [the repair guide](docs/running-campaigns.md#a-reflectors-first-request-is-too-large).

- **Unfinished `/15` runs from `2.0.0b3` or `2.0.0b4`.** Before upgrading, finish or stop (Ctrl+C) every `/15` run. Alongside the usual producing-version `rebase`, a `chat` Reflector needs a Reflector identity `rebase` because its tool schema becomes `reflect_tools/2`. Accept the reported differences through `evo tree authorize`, then resume with the same command. The helper extension changes no `agent_cli` or `subprocess` Reflector identity; packaged prompts and `reflect_request/2` are unchanged. The Editor brief, frozen Attempt inputs, artifact matcher and `role_projection/1` digest are unchanged; a suspended open Attempt can continue after the accepted rebase.

- `evo tree evolve` still admits only `/15`. `evo tree conformance` still reads every manifest it read in `2.0.0b4`, including `evolution-v2-manifest/5`, and run directories of every family stay readable. The `evo-target` scaffold still writes `/15`; `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent.

## [2.0.0b4] — 2026-10-16

- **A scorer metric too large for a float is reported as non-finite.** A metric such as the integer `10**400` could not be converted to a float, so the scorer call was reported as a process failure. It is now refused as `metric_nonfinite`, like `1e999`. Measurement journals still record `failure: "scorer_error"`. In a family-14 connection preflight the outcome moves from `connection_preflight_unavailable` (cause `process_failed`) to `connection_incompatible`: `connection_primary_nonfinite` for the primary metric, otherwise `connection_scorer_output_invalid` with cause `metric_nonfinite`. Both values already exist, so readers need no change. Migration step: none; a scorer must return finite numbers.

- **`corpus_inside_role_workspace` is documented, and library callers can check it first.** `evo tree evolve` refuses a corpus file inside the seed, the run directory or a declared program path, or one that is a declared prompt file, with `corpus_inside_role_workspace`; the code is now in the [manifest refusal table](docs/manifest-v2.md#refusals). The new `check_corpus_placement(manifest, run_root=..., environ=...)` in `evolution.tree.evolve` runs the same check without starting a run; it checks nothing for a schema older than `/12`. Python import paths are not part of the beta promise. The refusal itself is unchanged.

- **A Target's `describe()` may return `None`.** The optional `describe()` callback of a target-server backend that returns `None` reports nothing: the instance is created and its body has no `description`, as for a backend without the method. This was already the behavior; the [library reference](docs/target-server.md#the-optional-describe-callback) now says so, and the `DescribingInstance.describe` annotation is widened to `Mapping[str, Any] | None`. Nothing changes for a running Target.

- **Fix #241: Unicode corpus case IDs no longer crash Attempt feedback.** A case ID admitted by the campaign journal can now appear in the frozen exam set and paired-score feedback before the Editor starts. Existing valid ASCII feedback bytes and campaign identities are unchanged. An unfinished run still follows the usual producing-version authorization when resumed with a different package version.

- **Unfinished `/15` runs from `2.0.0b3` or earlier.** As before any upgrade, finish or stop (Ctrl+C) every `/15` run first. A `/15` run that is still unfinished is refused once with `run_identity_changed`, because the package version changed: accept the printed `rebase` with `evo tree authorize`, then run the same `evo tree evolve` command again. `2.0.0b4` changes no role's opening text or packaged prompt, not the Editor brief or its `BRIEF_VERSION`, not the artifact matcher or the `role_projection/1` digest, and, for ASCII case IDs, not the inputs an Attempt froze, so a run that was suspended or crashed after an Attempt froze its inputs is not caught: after the accepted rebase it continues that Attempt.

- `evo tree evolve` still admits only `/15`. `evo tree conformance` still reads every manifest it read in `2.0.0b3`, including `evolution-v2-manifest/5`, and run directories of every family stay readable. The `evo-target` scaffold still writes `/15`, without `verbatim_answers`; `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent. Nothing is tagged or published.

## [2.0.0b3] — 2026-10-15

- **The Editor learns its file budget and can recover from it.** A `chat` or `agent_cli` Editor's opening states `max_changed_files`; an `agent_cli` Editor's opening also says that `.evolution/VALIDATION.json` can report the budget. The packaged Editor prompts are unchanged, so a run that does not use the new features keeps its Editor identity. A `chat` Editor whose plan or edit would exceed it gets an ordinary tool error naming the count and the limit, and writing an updated file's original bytes back frees its slot (a deleted file cannot be restored this way). An `agent_cli` Editor that exceeds it spends its one repair call on it. Admission at the end still decides. The Reflector's opening states the Editor's budget too.

- **Editor file-budget refusals remain readable.** In family 14, an Editor refused on its file or byte budget records `editor_failed.reason` as `changed_file_budget` or `written_byte_budget` instead of `actor_violation`. Subsequent Attempt feedback and later-round history retain the same reason, so journal readers must recognize both values.

- **Critical fix: declare canned answers with `verbatim_answers`.** A `/15` manifest may list, in the new optional top-level key `verbatim_answers` (`verbatim_answers/1`), answers the agent must give word for word. Such a string, and any oracle value contained in it, is no longer treated as leaked oracle text by the Grouper and Reflector checks, attempt feedback or the Candidate seal; every other oracle value keeps its checks. New refusal codes: `verbatim_answer_unmatched` and `record_matches_secret` at `verbatim_answers.strings[i]`. Migration step: none for a manifest without the key; its identity and run are unchanged. To use it on an existing run, add the key and accept the `rebase` that `evo tree authorize` offers. 2.0.0b1 and 2.0.0b2 refuse a manifest with the key (`unknown_key`) and cannot resume or read a run that uses it.

- **Unfinished `/15` runs from `2.0.0b2` or `2.0.0b1`.** As before any upgrade, finish or stop (Ctrl+C) every `/15` run first. A `/15` run that is still unfinished is refused once with `run_identity_changed`, because the package version changed: accept the printed `rebase` with `evo tree authorize`, then run the same `evo tree evolve` command again. `2.0.0b3` changes the Editor's and the Reflector's opening text and the Editor's tool results, but not the Editor brief, its `BRIEF_VERSION` or the inputs an Attempt froze, so a run that was suspended or crashed after an Attempt froze its inputs is not caught: after the accepted rebase it continues that Attempt.

- `evo tree evolve` still admits only `/15`. `evo tree conformance` still reads every manifest it read in `2.0.0b2`, including `evolution-v2-manifest/5`, and run directories of every family stay readable. The `evo-target` scaffold still writes `/15`, without `verbatim_answers`; `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent. Nothing is tagged or published.

## [2.0.0b2] — 2026-10-15

- **A process log in the run directory.** `evo tree evolve` on a `/15` run and `evo tree evaluate` keep their terminal output under `logs/<command>-<UTC start>-<pid>.log`, created `0600` in `0700`. The log begins after the invocation's last refusal point, redacts the run's secret set and keeps an unhandled traceback and closing `exit` line. An owned Python fault handler sends fatal-signal and default-`SIGHUP` stack dumps there. A refused campaign, an unchanged completed evaluation, or a finished campaign reopen with no journal append gets no new log. Library calls get none; the log is not evidence. See [diagnosing a failed run](docs/running-campaigns.md#a-run-failed-start-here).

- **`--log-file PATH`** on `evo tree conformance` and `evo-target serve` appends the command's output to an owner-only file; an unsafe or unusable path refuses with `log_file_unsafe` or `log_file_unavailable` (exit 2).

- **A closed stdout or stderr no longer ends a logged invocation.** `evolve`, `evaluate`, and `conformance`/`serve` with `--log-file` stop writing to the failed stream and keep its output in the log. `SIGHUP` keeps its default action; use `nohup`, `setsid` or a service manager to outlive the terminal.

- **`logs` is an output root of a `/15` run directory.** An unsafe directory is `run_dir_unsafe_entry at logs`, accepted with `evo tree authorize` like the other roots; a file where the directory must be stays hard.

- **The `chat` Grouper repairs a full map request.** An invalid answer to a map request that filled `max_input_chars` was never repaired and fell back. Its repair is now sent as the original request with a note naming the defects, and map and reduce requests leave 600 characters for it. Map chunks are therefore slightly smaller, and a reduce is skipped slightly earlier. The private grouping log records each repair and its defects.

- **Only `/15` runs.** `evo tree evolve` creates and resumes campaigns only from `evolution-v2-manifest/15` (campaign family 14). It refuses `/5`–`/14` with `manifest_schema` (exit 2) for a new run, a resume and a reopen, and refuses a `/15` document pointed at a run directory of family 3 to 13 with `campaign_family_unsupported` (exit 2), before contacting the target. `run_campaign` and `resume_campaign` refuse families 6 to 13 with `campaign_family_unsupported`. The deprecation line and the library `DeprecationWarning` are gone.

- **Before you upgrade,** finish or stop every `/5`–`/14` run with `2.0.0b1`. An unfinished run cannot be resumed after the upgrade. Its directory stays readable, and the refusal writes nothing into it. If you upgraded too early, reinstall `2.0.0b1` to finish or stop the run.

- **Old runs stay readable.** `inspect_campaign(..., verify=True)`, `evo tree trace`, `evo tree watch`, `evo tree evaluate` and `evo tree authorize --list` work on run directories of every family. A `/15` campaign can link a finished `/14` or `/15` run. A finished run of family 5 to 12 can no longer be linked: start a new `/15` campaign from its champion copy, which does not inherit its lineage or statistical spend.

- **Provenance in the agent guide.** The agent integration guide explains how a Target reports provenance for the affected-case gate.

- **Unfinished `/15` runs from `2.0.0b1`.** As before any upgrade, finish or stop (Ctrl+C) every `/15` run first. A `/15` run that is still unfinished is refused once with `run_identity_changed`, because the package version changed: accept the printed `rebase` with `evo tree authorize`, then run the same `evo tree evolve` command again. `2.0.0b2` does not change how the files the roles see are selected or digested, so a run that was suspended or crashed after an Attempt froze its inputs is not caught: after the accepted rebase it continues that Attempt.

- `evo tree evolve` admits only `/15`. `evo tree conformance` still reads every manifest it read in `2.0.0b1`, including `evolution-v2-manifest/5`, and run directories of every family stay readable. The `evo-target` scaffold still writes `/15`; `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent. Nothing is tagged or published.

## [2.0.0b1] — 2026-10-13

- The first beta of Evolution 2.0. `evo tree evolve` admits `evolution-v2-manifest/15` and creates campaign family 14: the `stable` or `unstable` comparison with the Champion, the `first_accepted` or `all_attempts` round stop, the baseline refresh, the focus margin, exact comparisons, the Grouper role, the connection preflight and role visibility. Family-14 status, result and inspection documents add `final`, `roles`, `settings` and `connection`.

- Manifests `/13` and `/14` are deprecated from `2.0.0b1`. `evo tree evolve` still runs them and prints one line on stderr, `--quiet` included; a library caller of `run_campaign` or `resume_campaign` that creates or resumes campaign family 12 or 13 gets a `DeprecationWarning`. The `/5`–`/12` line now names `/15`, or `/13` for a linked successor. Nothing is journaled; results, reports and exit codes are unchanged.

- Release `2.0.0b2` refuses `/5`–`/14`: finish or stop every `/5`–`/14` run before upgrading to `2.0.0b2`. Their run directories stay readable.

- `evo-target init` writes `/15` with the recommended values. The demo's outcome is unchanged: round 1 promotes and round 2 closes clean. The result's caveats are now `champion_not_contemporaneous`, `focus_selection_bias`, `intervals_empirical` and `exact_contrast`.

- `evo tree evolve --help` lists the `/15` settings.

- Includes the read-only run observability introduced in `0.3.0a35`: `evo tree watch`, `evo tree trace` and opt-in `--progress`. See [watching a run](docs/running-campaigns.md#watch-a-running-campaign) and [exporting traces](docs/running-campaigns.md#export-what-each-case-did).

- New exports from `evolution.tree`: `SCHEMA_V15`, `CampaignPolicyV11`, `FINAL_CAMPAIGN_FAMILY_VERSION` and `ConnectionContract`.

- Upgrade rule for family 14: before any upgrade, finish or stop (Ctrl+C) every family-14 run.

- Known issues (maintainer triage 2026-10-13: nothing open is critical; all fixes come after the beta):

- `evo tree evolve` and `evo tree conformance` accept the manifests of `0.3.0a35`, and `evo tree evolve` also admits `/15`. Earlier manifests, including `evolution-v2-manifest/5`, keep their meaning and their run directories stay readable. The `evo-target` scaffold now writes `/15`; `package_version_mismatch` still requires explicit handling on resume. `evo gui`, the `[gui]` extra and `evo tree edit` remain absent. Nothing is tagged or published.
