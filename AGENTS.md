# Working with Evolution

Requests here are of two kinds. Someone wants to **use** Evolution: set up, measure or improve their own agent. Or someone wants to **change** Evolution itself. Decide which before you read code.

- To use Evolution, follow [Help someone use Evolution](#help-someone-use-evolution). The guides below describe each step; you do not need to read or change the source code.
- To change Evolution, start at [Work loop](#work-loop).

Start with the requested outcome, inspect the working tree, and preserve unrelated changes. Read the [README](README.md), [Quickstart](QUICKSTART.md), and [documentation index](docs/SUMMARY.md) before changing an unfamiliar area.

## Help someone use Evolution

Read the guide for a step before you write a file or run a command for it. Follow its layout and commands; do not reconstruct them from the source code. Evolution runs on Linux and macOS.

For direct campaigns, CI and recovery, start with the [core-campaigns skill](docs/core-campaigns/SKILL.md).

1. Install and run the offline example: [Quickstart](QUICKSTART.md). It needs no API key.
2. Connect the agent as a Target Service: [Connecting an agent](docs/connecting-an-agent.md), [Target server](docs/target-server.md) and the [reference target](docs/reference-target.md).
3. Write the manifest: [Manifest reference](docs/manifest-v2.md) and [Configuration](docs/configuration.md).
4. Run, follow, continue and measure held-out cases: [Running campaigns](docs/running-campaigns.md) and the [CLI reference](docs/cli.md).

The skill [`docs/evolving-skills/SKILL.md`](docs/evolving-skills/SKILL.md) covers a different workflow: improving one skill file against pytest tasks with `evo skill`, `evo run` and `evo evolve`. Use it only for that workflow, not for campaigns.

Ask before these actions:

- Spending money: a campaign with model-driven roles or a paid agent. State the expected calls and the cost cap first.
- Authorizing a refused continuation, or starting, resuming or deleting a campaign. Propose the exact command and let the person run it or confirm it.
- Keep API keys in the environment or the person's credential store. Never write them into project files, manifests or commits.

## Work loop

1. Read the implementation, its callers, and the relevant tests before editing.
2. Make the smallest change at the boundary that owns the behavior.
3. Keep user documentation, command help, and tests consistent with supported behavior.
4. Run the checks that cover the changed area, then report the exact command and result.

Use [Contributing](CONTRIBUTING.md) for setup, the supported offline checks, package checks, and contribution expectations. The [CLI reference](docs/cli.md) owns command syntax and exit behavior. [Configuration](docs/configuration.md) and the [manifest reference](docs/manifest-v2.md) own supported settings.

## Code map

The installed package is under `src/evolution/`. The task and skill workflow uses `core/`, `eval/`, `evolve/`, `lineage/`, `llm/`, and `cli/`. Tree campaigns live in `src/evolution/tree/`: storage, execution, artifacts, manifests, commands, policy, and campaign orchestration. Reuse the nearest existing module and its tests before adding a new abstraction.

Read [Architecture](docs/architecture.md) for campaign roles and data flow. Read [Running campaigns](docs/running-campaigns.md) for starting, inspecting, resuming, and continuing a campaign. Read [Run storage](docs/run-storage-layout.md) before changing persisted paths, journals, artifacts, or recovery.

## Campaign invariants

- The manifest defines campaign semantics. Command presentation does not override it.
- Keep the seed, role programs, and run directory separate. Promotion keeps a selected copy under the run directory.
- Journals and the artifact ledger are durable facts. Preserve their identity and recovery checks; do not edit a run's records by hand.
- A target or infrastructure failure is distinct from a candidate-quality result. Preserve retry, suspension, and resumption behavior.
- Keep raw oracle data and held-out evidence out of editor inputs and public feedback.

For target integration, use [Connecting an agent](docs/connecting-an-agent.md), the [target server guide](docs/target-server.md), and the [reference target](docs/reference-target.md). For train/test partitioning and standalone evaluation, read [Running campaigns: train and test](docs/running-campaigns.md#improve-on-train-and-measure-on-test) before changing corpus handling or measurement.

## Task and skill workflow

Use [Manual workflow](docs/manual-workflow.md) for task and skill commands, and [manual architecture](docs/manual-architecture.md) for their stored state and boundaries. Use [Custom tasks](docs/custom-tasks.md) and [API usage](docs/api-usage.md) when changing integrations or examples.

## Secrets and generated data

Keep credentials in the process environment or the operator's credential system. Do not add credentials, model output, generated runs, traces, or task data to tracked files. [Security](SECURITY.md) describes the trust model and vulnerability reporting. Use synthetic fixtures for offline tests, and keep functional prompt and fixture bytes unchanged unless the task explicitly changes them.
