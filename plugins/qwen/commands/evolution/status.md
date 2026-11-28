---
description: Read the current status of a Lab operation. Pass its operation ID.
---

Read the status of one Lab service operation. Use the command argument as the operation ID. If it is missing or contains more than one argument, ask for a single ID and stop.

Run `lab status -- OPERATION`, passing the ID as one literal argument. If the user specified a Lab service or the current workspace has a `socket` in `.lab-workspace.json`, use that service. Put `--socket` or `--home` before `status` and pass its path as one argument. If `lab` is unavailable on PATH and you are working in a Lab source checkout, run `./.lab/venv/bin/lab` from its `lab/` directory with the same arguments.

Keep the interaction read-only.

Return only these status fields, in a short summary:

- State, process exit and cleanup, as reported by Lab.
- Progress: phase, current and planned round, and scored and expected samples, when reported. `round` is current/planned. A dash means Lab supplies no value for that field in this snapshot. For `round=-/2`, say “Lab shows no current round. Two rounds were planned.” For `scored=-/-`, omit sample counts.
- Saved reason and next action, when Lab supplies them. For a suspension, include the `hint` and `continuation` when present.

Leave the raw `attempts` field out of the summary. Use the campaign report for completed rounds, attempt history and measurement coverage. If status is unavailable, give the CLI error instead.

Operation ID

{{args}}
