---
description: Read and summarize a Lab operation report. Pass its operation ID.
---

Read the report for one Lab service operation. Use the command argument as the operation ID. If it is missing or contains more than one argument, ask for a single ID and stop.

Run `lab report --markdown -- OPERATION`, passing the ID as one literal argument. If the user specified a Lab service or the current workspace has a `socket` in `.lab-workspace.json`, use that service. Put `--socket` or `--home` before `report` and pass its path as one argument. If `lab` is unavailable on PATH and you are working in a Lab source checkout, run `./.lab/venv/bin/lab` from its `lab/` directory with the same arguments.

Read the Markdown from stdout and keep the interaction read-only.

Summarize the outcome or available measurements. Include scored and expected sample counts where reported, and call out partial coverage, a pending outcome or other limits on what the report supports. If the report is unavailable, give Lab's reason when available.

Read round history from both the attempt table and the rounds-without-attempts table (`Раунды без попыток`). Count each recorded round once, including rounds without attempts. Report `clean` as a round outcome: the parent measurement found no failing cases, so the round closed without opening an attempt. Report process or target cleanup separately if the output provides it. A dash in a table cell means no value was reported for that field.

Operation ID

{{args}}
