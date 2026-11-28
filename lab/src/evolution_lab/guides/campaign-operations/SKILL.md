---
name: evolution-lab-campaign-operations
description: Use when monitoring a running Lab campaign, diagnosing a suspension or refusal, resuming an operation, or starting a successor from its Champion.
---

# Campaign operations

Identify the campaign, its producing Lab and Core pair, and how it was launched. For a registered project's next new cycle, use [evolve](../scenarios/evolve/SKILL.md).

- **Service operation.** Use `lab status OPERATION` for a snapshot or `lab watch OPERATION` until the run needs attention or ends. Use `lab list --summary` to find the ID. Use the original service and put `--socket SOCKET` or `--home HOME` before the command when needed. If the ID is unavailable, verify the endpoint and producing pair before interpreting the run.
- **Directory run.** Read the printed run directory under `DIR/.lab/runs/`. Match the campaign's operation ID in `summary.json` and read `campaign.log` and `report.md` when present. Evaluation and campaign share run numbers. Use the campaign's producing wheels and `DIR/.lab/state-<pair>`.

Stopping `lab watch` ends observation. Interrupting a foreground `lab campaign DIR` stops that invocation's work.

## Read the state and choose the next action

| State | Action |
|---|---|
| Running | Report the current start and recorded progress. Keep watching when the user asks to follow it through. |
| Suspended | Read the saved reason and evidence. For a service operation, follow `hint:`, `continuation:` and `resume` in the current status. Confirm cleanup before resuming when required. Follow the fresh target requirement if status lists excluded targets. See [suspension](../campaign-package/SKILL.md#suspension), [cleanup](../diagnostics/SKILL.md#cleanup-required) and [provider routing](../budget/SKILL.md). |
| Core refused | Read every finding from the latest start. Use `refusal.json` when present or the current service status. Present the allowed remedies to the person. Propose `--accept CODE:DIGEST --reason TEXT` only for an offered acceptance. Include the offered `--effect` when the person selects one. |
| Finished | Report the Core outcome, persistence and cleanup separately. Use [reports and diffs](../diagnostics/SKILL.md#report) for result evidence. If asked to build on the final Champion, verify the predecessor has retained results. |
| Abnormal or unreadable | Read current status or the command's saved failure and log. Report the failure stage, persistence and confirmed cleanup separately. |

Use exit codes with their command. `lab watch` exits `0` when finished, `3` when suspended and `4` on an abnormal end. If observation is interrupted or exits `1`, read status to establish the latest state. `lab campaign DIR` exits `0` for promotion, `1` for a finish without promotion, `2` for input or preparation error, `3` for target, execution, persistence or cleanup failure, `4` for suspension and `5` for a Core refusal. Read the saved reason or outcome before drawing conclusions.

## Propose continuation or a successor

| Intent | Directory run | Service operation |
|---|---|---|
| Continue the same operation | `lab campaign DIR --continue` selects the latest campaign for this project and pair with its saved prepared inputs. After edits, add `--reprepare` to prepare current files or `--prepared ID` to select a saved preparation. | Use `lab resume OPERATION` with the target, generation and secrets requested by status. Use `--prepared ID` to select a different prepared execution. |
| Start a linked successor from the final Champion | `lab campaign DIR --from OPERATION` | `lab start PREPARED --linked-from OPERATION` with the required target, generation and secrets. |

Check the intended operation before proposing the command. Directory `--continue` selects the latest campaign for the pair. To address another operation, use its producing service and explicit operation ID when that route is available. Supply both producing wheels via `--wheel` or use `just campaign DIR` with the same options from the matching Lab checkout.

Lab supports explicitly changing the prepared execution and linking starts from dev75/b19. The linked predecessor must be in the same Lab state. Core decides admission for changed inputs and for the predecessor's family and mode. A linked successor defaults to the final Champion and asks Core to inherit compatible baseline evidence. Set linkage on the start command.

Monitoring is read-only. Present cleanup or stop separately when needed. At `ask` in a Lab workspace, starts, resumes, accepting refusals and other asking actions need the human's confirmation. Use the MCP tool so Lab can ask. With the CLI, give the expected calls and cost cap for paid actions, ask in the conversation, and run the command after a yes. At `yolo`, run the actions the human requests. Outside a Lab workspace, follow the approval rules for the command you are using.
