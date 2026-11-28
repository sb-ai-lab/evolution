---
name: evolution-lab-scenario-evolve
description: Run evolution campaigns on a connected agent, one cycle per campaign — plan, prepare, run, collect the result — and know when each step is done.
---

# Scenario `evolve`: evolve the agent

The scenario is available once the implementation is **connected** (scenario
`evolution-lab-scenario-connect`). Every campaign is one cycle: plan, prepare, run,
result. The status shows the next cycle and every earlier one.

Before you start:

- Service commands talk to a running `lab serve --home <home>`. Put the global option
  `--socket <path>` before the command name when the service is not found by default.
- Project commands below name the implementation with `--implementation <implementation>`;
  while the project has one active implementation the flag may be omitted.
- Start from the status and return to it after every step:

```bash
lab project status <project> --implementation <implementation> --scenario evolve
```

- `next_cycle` shows what the next campaign still needs; `cycles` lists campaigns of this
  implementation, the current one first. A cycle of a previous Lab version is shown with
  `detail: unavailable` from its last snapshot.
- To follow an existing run, use `lab watch <operation-id>` and [campaign operations](../../campaign-operations/SKILL.md) for the transitions after a pause, refusal or finish.
- Guides are named by skill name: `evolution-lab-<name>`, with an anchor after `#`. With
  only the CLI, print where the guides are installed and open `<name>/SKILL.md` there (the
  name without the `evolution-lab-` prefix) at the anchor after `#`:

```bash
lab guides path
```

- Follow the autonomy in the workspace's `AGENTS.md`. Steps marked `human_only` need the human's
  confirmation at `ask`. Use the MCP tool so Lab can ask. With the CLI, ask in the conversation
  and run the command after a yes. At `yolo`, run the requested steps.
- A step is done only when `lab project status` says `done` — not when a command exits 0.

<a id="plan"></a>
## Step `plan` — plan the campaign

Goal: the package entry names a campaign manifest of the pinned core schema, with the
editable scope, scale and budget you decided on.

- Editable scope: `evolution-lab-campaign-package#editable-scope`.
- Editor and reflector: `evolution-lab-campaign-package#editor-and-reflector`.
- Scale and retries: `evolution-lab-campaign-package#campaign-scale`.

```bash
lab project facts <project> --implementation <implementation>
```

Done when: step `plan` of `next_cycle` is `done` (the manifest passes the form check).

<a id="prepare"></a>
## Step `prepare` — prepare the campaign

Goal: a campaign PreparedExecution of this Lab version on the current inputs.

Write the request file **outside the working copy** — in the project directory next to
`AGENTS.md` (for example `campaign-request.json`); a file inside the working copy changes the
implementation's inputs and makes check, conformance and baseline stale. The request is a
closed form: every key below is required, `null` means "Lab's default".

```json
{"kind": "campaign", "bundle_digest": "<digest from lab project bundle>",
 "stop_grace_seconds": null, "cleanup_wait_seconds": null}
```

```bash
lab project bundle <project> --implementation <implementation>
lab project prepare <project> --implementation <implementation> --request campaign-request.json
```

If Lab answers `invalid_request`, its message names the missing keys.

Done when: step `prepare` of `next_cycle` is `done`. Any later edit of the consumed
inputs makes it `ready` again.

<a id="run"></a>
## Step `run` — run the campaign

**Needs the human's confirmation at `ask`.** The campaign calls models and may take long.

Goal: the campaign has run to a terminal state.

```bash
lab project target <project> --implementation <implementation> --bundle <bundle-digest>
lab job status <task-id> --generation <owner-generation>
lab project start <project> --implementation <implementation> --prepared <prepared-digest> --target <target-id>
lab status <operation-id>
lab events <operation-id>
lab stop <operation-id>
```

Wait for the target task: repeat `lab job status` until it is `succeeded`, then start.

A suspended campaign is a finished start that can continue; its cycle shows
`resume.state` (`evolution-lab-pitfalls#suspended-owner-campaign`,
`evolution-lab-campaign-package#suspension`):

```bash
lab resume <operation-id> --target <target-id> --generation <owner-generation>
```

If `resume.target` is set (after a `target_unstable` suspension), continue on a fresh
target: stop the excluded one first (`--wait`, so it is really stopped before you start), start a new one of the same Bundle, and resume on it.
`resume.target.excluded` lists every target the campaign has used; resuming on one of them
is refused with `target_unstable_fresh_required`.

```bash
lab agent stop <excluded-target-id> --generation <owner-generation> --wait
lab project target <project> --implementation <implementation> --bundle <bundle-digest>
lab resume <operation-id> --target <new-target-id> --generation <owner-generation>
```

Cleanup before continuing: `evolution-lab-diagnostics#cleanup-required`.

Done when: step `run` of the cycle is `done` — core's projection says the campaign is terminal (the cycle's
`outcome` is set). A suspended campaign is not terminal: the step is `blocked` with
`blocked_reason.code: campaign_suspended` and the cycle shows `resume`; when Lab cannot read the projection the
code is `campaign_state_unknown`. The cycle is `complete` when every non-optional step is `done`.

<a id="result"></a>
## Step `result` — collect the result

**Optional.** The cycle is complete without an export; export when you need the change set.

Goal: the change set of the current start is exported.

```bash
lab report <operation-id>
lab diff <operation-id>
lab export <operation-id> --wait
lab export-content <operation-id> <export-id> --output /absolute/path/change-set.zip
```

Reports, diff and export: `evolution-lab-diagnostics#report`,
`evolution-lab-diagnostics#diff-export`. After a resume, the export of the previous
start does not count: the step is not done until the new start is exported, and while the
resumed start runs it is `blocked` on `run`. Delivering the change to the team repository
is a person's action; Lab never pushes.

Done when: step `result` of the cycle is `done`.
