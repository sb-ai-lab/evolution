# Evolution documentation

Start here to use Evolution. These guides describe the v2 tree-evolution beta in this checkout, `2.0.0b16`. You can run the first example without an API key or a separate agent project. Its campaign manifest is `evolution-v2-manifest/15`, the scaffold format; opt-in `/16` adds independent acceptance controls.

## Learn by running a campaign

1. [Overview](../README.md) — what Evolution does and which workflow to use.
2. [Your first campaign](../QUICKSTART.md) — install, run the example and inspect its selected files.
3. [How Evolution works](architecture.md) — the roles, campaign cycle and evidence behind a promotion.
4. [Running campaigns](running-campaigns.md) — prepare your own experiment, interpret results and recover from failures.
5. [Connecting your agent](connecting-an-agent.md) — wrap your agent with the target server library.

## Report problems or propose changes

See [Contributing](../CONTRIBUTING.md) for issue reports, pull requests and local checks, [Security](../SECURITY.md) for private vulnerability reports, and [Changelog](../CHANGELOG.md) for delivered changes.

## Look up a detail

| Topic | Reference |
|---|---|
| Commands, output and exit codes | [CLI](cli.md#tree-service-commands) |
| Train/test preparation and before/after measurement | [Train/test workflow](running-campaigns.md#improve-on-train-and-measure-on-test) |
| Campaign JSON settings and role protocols | [Manifest v2](manifest-v2.md) |
| Generated demo files and their exact contents | [Reference target](reference-target.md) |
| Backend callbacks, seeds and server lifecycle | [Target server library](target-server.md) |
| A run failed and you need to find the cause | [A run failed: start here](running-campaigns.md#a-run-failed-start-here) |
| Run files, logs and inspection | [Run storage layout](run-storage-layout.md#the-v2-run-directory) |
| Live progress and status snapshots | [Watch a running campaign](running-campaigns.md#watch-a-running-campaign) |
| Export case inputs, replies and scores | [Trace export](running-campaigns.md#export-what-each-case-did) |
| Choose `/16` measurement, acceptance and verification | [Independent acceptance](running-campaigns.md#choose-independent-acceptance-manifest-16) |
| Choose `/15` comparison and stopping settings | [Stable family](running-campaigns.md#judge-a-candidate-with-the-stable-family-manifest-15) |
| Upgrade an existing installation | [Upgrade and recovery rules](running-campaigns.md#resume-after-a-failure), [beta known issues](../CHANGELOG.md) |
| Accepting a detected input change | [Operator authorizations](running-campaigns.md#accept-a-reported-input-change) |
| Sample retries, exclusions and linked runs | [Incomplete evidence](running-campaigns.md#understand-incomplete-evidence) |
| Role environment and command binding | [Command environment](configuration.md#campaign-role-environment) |
| Terms used across the repository | [Campaign terms](architecture.md#one-campaign-step-by-step) |

## The separate task and skill workflow

These guides cover `evo task`, `evo skill`, `evo run`, `evo trace` and `evo evolve`. They use saved skill versions and task traces, rather than the Target Service and campaign manifest described above.

- [Manual workflow](manual-workflow.md) — bootstrap a task, solve, reflect, rewrite and optimize.
- [Manual workflow architecture](manual-architecture.md) — immutable bindings, traces, validation and the edit brake.
- [Python API](api-usage.md) — the manual workflow and optimizer functions.
- [Configuration](configuration.md) — environment variables and options for these commands.
- [Custom tasks](custom-tasks.md), [task imports](after-bench-import.md) and [split manifests](splits.md).
- [Child-process environment](child-process-environment.md).
- [Trace analysis](evolving-skills/trace-analysis.md), [lifecycle reference](evolving-skills/reference.md) and [prompt reference](prompts_examples.md).
