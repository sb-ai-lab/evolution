# Trace Analysis

## Goal

Use saved traces to identify reusable gaps in a skill body. Propose a targeted change. Do not use held-out validation or test evidence in an editing prompt.

## Inspect A Trace

Run `evo trace list SKILL` to select a trace. Run `evo trace show SKILL TRACE_ID --section result` to inspect its result.

Each `result.json` stores `passed`, `total`, `pass_rate`, `success`, `tokens_in`, `tokens_out`, `time_ms`, and `errors`. Read `prompt.md`, `response.md`, and `solve.py` only when the evidence boundary permits it.

Read `evidence.json` when it exists. Its `failure_evidence.failure_categories` field records safe diagnostic categories. Do not treat a missing evidence packet as a successful trace.

## Group Failures

Group failing traces by the recorded category and by repeated behavior. Use the categories below as evidence labels, not as invented `result.json` status values.

| Category | Meaning |
|---|---|
| `timeout` | The task run exceeded its time limit. |
| `runtime_exception` | The run raised an exception. |
| `nonzero_exit` | The solution process returned a nonzero exit code. |
| `code_extraction_failed` | The solver did not provide usable code. |
| `no_artifact` | The run produced no recorded output artifact. |
| `invalid_output_shape` | A recorded output did not match its expected shape. |
| `produced_but_failed_verification` | The run produced output, but verification failed. |

`llm_transport_failed` and `model_transport_failed` are solver or optimizer diagnostics. Check `errors` and optimizer state for those failures.

## Compare The Skill Body

Choose a repeated pattern. Compare the skill body with the observed action and result. Identify one missing, incorrect, or ambiguous instruction.

Check the execution contract, required files, output format, tool guidance, and common failure conditions. Keep examples general. Do not add task answers, test literals, or private paths.

## Propose A Change

Name the exact heading to change. State the observed pattern and the smallest guidance change that addresses it. Keep unrelated wording unchanged.

Use a manual rewrite or the Evolution editor. A finalized candidate can receive `candidate_edit_budget_exceeded`, `leak_rejected`, or another controlled rejection. Only an edit-brake rejection can start another editor attempt.

## Verify The Change

Measure a finalized candidate on the approved training evidence. Use held-back validation only for selection. Record test results separately from development evidence.

For the complete trace and rewrite contracts, see [the deep reference](reference.md), [the CLI reference](../cli.md), and [the manual workflow architecture](../manual-architecture.md).
