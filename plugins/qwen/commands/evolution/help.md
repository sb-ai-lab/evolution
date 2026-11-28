---
description: Pick the right Evolution skill and show the next step.
---

Match the request to a workflow and show its next step. Use the skill name registered by the host, including its extension prefix when present.

| Goal | Skill |
|---|---|
| Measure a first baseline from a Python agent directory | `evolution-lab-first-evaluation` |
| Connect and measure a registered Lab project | `evolution-lab-scenario-connect` |
| Plan a campaign for a connected project | `evolution-lab-scenario-evolve` |
| Observe or continue a Lab campaign | `evolution-lab-campaign-operations` |
| Use Core directly with `evo tree` or `evo-target` | `core-campaigns` |
| Improve a single skill against pytest tasks | `evolving-skills` |

For a Lab service operation, use `/evolution:status OPERATION` to inspect its current state or `/evolution:report OPERATION` to read its report.

In a Lab project workspace the `evolution-lab` MCP tools are configured; its `AGENTS.md` says how to use them.

Give guidance only. When the user gives no task, list the workflows.
