# Prompt Reference

This page maps active Evolution prompt assets to their routing modules. The prompt assets are executable Markdown, and the table below names the active routes.

## Edit Brake Guidance

Every Evolution editor receives the exact effective normalized-line-churn budget. The editor must make the smallest sufficient edit. The system applies the shared edit brake only after finalization and before validation or promotion.

An edit-brake rejection can start another editor call when `--max-rewrite-attempts` permits it. The retry uses the same immutable parent and bounded feedback. The feedback gives churn and advisory character measurements. It never includes a rejected candidate body or held-out evidence.

Absolute character and non-empty-line ceilings remain separate checks. An oversized candidate is rejected. The system does not make a compaction call.

## Active Assets

| Asset | Module that renders it | Purpose |
|---|---|---|
| `system_reflect.md`, `reflect_failure.md`, `reflect_success.md` | `evolution.evolve.reflection` and research editor paths | Diagnose eligible trace evidence. |
| `legacy/rewrite_system.md`, `legacy/rewrite_prompt.md` | `evolution.evolve.rewrite_text_return` | Produce a text-return candidate body. |
| `legacy/codex_file_edit_system.md`, `legacy/codex_file_edit_prompt.md` | `evolution.evolve.rewrite_file_edit` | Edit `SKILL.md` in an isolated workspace. |
| `legacy/session_system_*.md`, `legacy/session_prompt.md` | `evolution.evolve.rewrite_session` | Run an agentic editing session. |
| `system_edit.md`, `propose_edits*.md`, `merge_edits.md` | `evolution.evolve.rewrite_edit_ops` | Propose, merge, and apply structured edits. |
| `agentic_wrapper_session.md` | Research-only `iterative_evolution.agent_session_edit` | Request structured edits from a session editor. |
| `agentic_wrapper_hermes.md` | Research-only `adapters.hermes_adapter` | Request structured edits from the Hermes editor. |

The active compatibility assets under `legacy/` remain active because the rewrite modes load them. The two compact prompt files in that directory are historical records. No active route loads them.

## Prompt Inputs

The rewrite modules supply the current skill body, task context, allowed evidence, and absolute-size ceilings. Structured editors also receive the edit-operation limit from `EVO_EDIT_BUDGET` and the exact edit-brake policy. `EVO_EDIT_BUDGET` limits operation count. It does not limit normalized churn.

The file-edit and agentic-session modes record new headings as advisory evidence. No mode rejects a candidate solely because it adds a heading. Format, frontmatter, filesystem, absolute-size, leakage, and shared-brake checks remain in force.

## Safe Editing Rules

Prompt assets must preserve the train, validation, and test evidence boundary. Do not put held-out validation or test evidence in an editing prompt. Do not add a private relative churn gate or a relative size target to one mode.

After changing a runtime prompt, update [`tests/test_prompts_golden.py`](../tests/test_prompts_golden.py) and run its focused regression test. For policy behavior, see [architecture.md](architecture.md), [configuration.md](configuration.md), and [run-storage-layout.md](run-storage-layout.md).
