# Evolution

Evolution helps you improve the files an agent uses: skills, prompts and other editable instructions. You provide an agent, evaluation cases and a scoring program. Evolution runs the current version, investigates failures, proposes edits and compares each candidate with the current best version. It keeps a candidate only when the configured checks support the change.

This branch contains the **v2 tree-evolution beta, `2.0.0b16`**. The workflow uses a local Target Service to run your agent. No model weights are trained. Your original source tree stays unchanged; candidates, selected versions and the run history live in a separate run directory.

## Start here

If you are joining the project, follow these guides in order:

1. [Run your first campaign](QUICKSTART.md) — install this version and run a complete example without an API key.
2. [Understand the workflow](docs/architecture.md) — what the target, scorer, Grouper, Reflector and editor do.
3. [Run campaigns on your own tasks](docs/running-campaigns.md) — prepare inputs, read results, recover from failures and continue experiments. If a run stopped unexpectedly, see [A run failed: start here](docs/running-campaigns.md#a-run-failed-start-here).
4. [Connect your agent](docs/connecting-an-agent.md) — implement the small backend that lets Evolution run your agent.
5. [Contribute to Evolution](CONTRIBUTING.md) — find the code, run checks and make your first change.

See the [documentation index](docs/SUMMARY.md) for reference pages.

## Try it from this checkout

Use Python 3.11.3 or newer. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[tree]"
evo --version
evo-target init ./demo
evo-target serve ./demo/manifest.json
```

Leave the server running. In a second terminal, from the same repository root:

```bash
source .venv/bin/activate
evo tree conformance --manifest ./demo/manifest.json
evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1
```

The example uses a small deterministic agent and scripted roles. It exercises the whole campaign without an LLM or provider credentials. The [Quickstart](QUICKSTART.md) explains the generated files, expected output and how to inspect the selected version.

Install this beta from the checkout or a wheel supplied by your team. A plain `pip install evo-skills` may install a different release; it does not identify the version documented here. Installation from a wheel is covered in the Quickstart.

## How a campaign works

You describe a campaign in one JSON **manifest**: which files may change, how to run the agent, which cases to evaluate, how to score them, and the time and attempt limits.

Evolution then repeats this cycle:

1. Measure the current best version, called the **Champion**.
2. Ask the **Grouper** to group observed failures, then the **Reflector** to investigate a group and suggest what to improve.
3. Ask the **editor** to change a copy of the round’s starting version, called the **Parent**, creating a **Candidate**.
4. Compare the Candidate with the Parent on the cases it aims to improve and other cases its edit can affect. A Candidate that passes becomes a **finalist**.
5. Measure the finalist afresh across the training corpus and compare it with the Champion. Promote an accepted finalist, or keep the current Champion.

The operator starts the Target Service; Evolution connects to it. The scorer defines task quality. The Grouper organizes failures, the Reflector diagnoses them, and the editor changes files. These can use different programs or models.

The run directory records what happened and preserves the selected files. The example writes `evolution-v2-manifest/15`; opt-in `/16` provides independent acceptance controls. Only these two file schemas and campaign families 14 and 15 are supported. Read removed-family runs with `2.0.0b9`, or seed a new unlinked `/15` campaign from the Champion copy. Infrastructure problems suspend a current campaign so you can repair the cause and run the same command again. A Grouper, Reflector or Editor failure not proven to be about its content also suspends rather than silently closing a round or Attempt. A completed current-family campaign continues through a linked run in a new directory. [Running campaigns](docs/running-campaigns.md) explains the difference.

The scaffold compares finalists with the stored Champion measurement (`stable`) and stops each round at its first accepted finalist (`first_accepted`). Use [the stable-family guide](docs/running-campaigns.md#judge-a-candidate-with-the-stable-family-manifest-15) to choose these settings. Before upgrading an existing installation, read the [upgrade and recovery rules](docs/running-campaigns.md#resume-after-a-failure) and the [beta’s known issues](CHANGELOG.md).

## Other workflows in the package

The package also includes the manual task/skill workflow: `evo quickstart`, `evo run solve`, `evo evolve reflect`, `evo evolve rewrite` and the task-based optimizer. These commands use tasks, saved skill versions and traces instead of a Target Service. They remain supported; use the [manual workflow guide](docs/manual-workflow.md) when that is the workflow you need.

## Reference

| I need to… | Read |
|---|---|
| Look up a command or exit code | [CLI reference](docs/cli.md#tree-service-commands) |
| Change campaign settings | [Manifest reference](docs/manifest-v2.md) |
| Understand the demo | [Reference target](docs/reference-target.md) |
| Implement the agent backend | [Target server API](docs/target-server.md) |
| Find run artifacts or logs | [Run storage layout](docs/run-storage-layout.md#the-v2-run-directory) |
| Check a target without a full campaign manifest | [Conformance-only manifest](docs/manifest-v2.md#what-a-1-document-is-still-for) |

## License

Apache License 2.0. See [LICENSE](LICENSE).
