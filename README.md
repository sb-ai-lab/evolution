![Evolution — the film’s ring-and-dot mark connected by branching paths.](docs/assets/readme/evolution-header.png)

# Investigate. Edit. Compare.

[English](README.md) · [Russian](README.ru.md)

Turn your agent’s failures into proposed changes to its skills and prompts. Evolution evaluates each change against your cases before selecting a version, and keeps the current version when no change is better.

**[Try it without an API key →](QUICKSTART.md)** · [Connect your agent](docs/guide/connect-agent.md) · [How it works](docs/guide/concepts.md)

**Use Evolution with your coding assistant.** Ready-to-use skills help it connect the agent you want to evaluate, set up an evaluation, run a campaign and read the results. [Build and install the plugin](plugins/README.md#build-and-install).

![Illustrative workflow: investigate an ungrounded answer, edit a skill, reject one Candidate, select another, then use the selected instructions to query data and answer the same task.](docs/assets/readme/evolution-film.gif)

[View the static example](docs/assets/readme/evolution-film-poster.png).

## One product, two packages

**Evolution Lab** (`evolution-lab`, in [`lab/`](lab/README.md)) is the recommended way to run Evolution. It turns your agent project into a reproducible experiment: it freezes the inputs, runs your agent as a local service, checks that it speaks the protocol, caps LLM spending, runs an evaluation or a campaign and gives you a report, a diff and an export. Everything works from the `lab` command and the Python API; a browser panel is optional.

**Evolution Core** (`evo-skills`, with the `evo` and `evo-target` commands) owns the campaign itself: the rules, measurement, journals, comparison gates and promotion. Lab runs Core for you. Use Core directly when you write the manifest and the agent service yourself, run campaigns from CI or embed them in your own tool: start at [Evolution Core](docs/core.md).

## Before you start

- **Linux or macOS.** On macOS, a process that leaves the group, escapes the tree between polls and hides the environment marker can survive a stop. Windows is not supported.
- **Python 3.12 or 3.11** (3.11.4 or newer). uv downloads the interpreter if it is missing.
- **[uv](https://docs.astral.sh/uv/)**, **[just](https://just.systems/)** and **git**.
- Read access to this repository. The [installation guide](docs/guide/install.md) lists every requirement and the manual install.

## First run

About five minutes. After setup, the demo runs offline: no LLM, no network access and no API keys.

```sh
git clone https://github.com/sb-ai-lab/evolution.git
cd evolution/lab
just setup
just demo
```

`just setup` builds Core and Lab wheels from this checkout and installs both into `lab/.lab/venv`. `just demo` measures a stub agent with a complete fixed evaluation and prints each question with the agent's answer and score. [The demo](docs/guide/demo.md) explains what you see and which files to open.

The [Quickstart](QUICKSTART.md) continues with your own agent and your first campaign.

## A better instruction starts with a specific failure.

The agent gives general retention advice without checking product data. Investigation identifies the missing query. The proposed skill now says to query the data and compare retention cohorts. Evolution tests the Candidate; your checks decide whether it is selected.

You supply the agent, editable files, cases and scorer. Evolution changes instruction files without training model weights. Training evidence guides edits and selection; held-out cases measure the fixed selected version separately. An infrastructure error never counts as agent quality, and "no improvement" is a valid answer.

## Keep going

- [The user guide](docs/guide/README.md): connect your agent, design the evaluation, run a campaign, read the results and control spending.
- [How Lab works](docs/guide/concepts.md): targets, Bundles, version pairs and conformance.
- [Evolution Core](docs/core.md): Core's own commands, manifest and run files, for direct and embedded use.
- [Browse all documentation](docs/SUMMARY.md), [contribute](CONTRIBUTING.md) or read the [release notes](CHANGELOG.md).

Core beta `2.0.0b22` · Lab `0.0.3.dev81` · [License](LICENSE)
