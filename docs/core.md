![Evolution — the film’s ring-and-dot mark connected by branching paths.](assets/readme/evolution-header.png)

# Evolution Core

[English](core.md) · [Russian](core.ru.md)

Turn your agent’s failures into proposed changes to its skills and prompts. Core evaluates each change against your cases before selecting a version.

Core, the campaign engine, is the `evo-skills` package with the `evo` and `evo-target` commands. It owns the campaign rules, measurement, journals, comparison gates and promotion. You supply the agent behind a Target Service, the editable files, the cases and the scorer. Core preserves the selected files and the run history, and a campaign can keep the current version.

Python 3.11.3+ · Beta `2.0.0b22` · [Apache 2.0](../LICENSE)

![Illustrative workflow: investigate an ungrounded answer, edit a skill, reject one Candidate, select another, then use the selected instructions to query data and answer the same task.](assets/readme/evolution-film.gif)

[View the static example](assets/readme/evolution-film-poster.png).

<a id="when-to-use-the-engine-directly"></a>

## When to use Core directly

Use Core directly when you write the manifest and the Target Service yourself, run campaigns from CI or another program, or embed campaign evaluation in your own tool. The commands, the manifest and the run files on these pages are the complete interface; nothing else is required.

## Install

```bash
git clone https://github.com/sb-ai-lab/evolution.git
cd evolution
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[tree]"
evo --version
```

Expect `evo 2.0.0b22`. The `tree` extra installs the campaign commands and the Target Service helpers. A plain PyPI install may resolve to another release. [Your first campaign](core-quickstart.md#1-install-the-beta) also covers installing a supplied wheel.

## Run your first campaign

[Your first Evolution campaign](core-quickstart.md) runs a complete offline example without an API key: create the example, start its Target Service, check the connection, run the campaign and read the selected files.

<a id="use-the-engine"></a>

## Use Core

- [Core campaigns skill](core-campaigns/SKILL.md) guides coding assistants through direct campaigns, CI and recovery.
- [Running campaigns](running-campaigns.md): prepare an experiment, follow and inspect a run, measure held-out cases and recover from a failure.
- [Connecting an agent](connecting-an-agent.md) and the [Target Service backend](target-server.md): what your agent's wrapper implements.
- [Manifest reference](manifest-v2.md): every campaign setting.
- [CLI reference](cli.md): every command, its output and its exit codes.
- [Run storage layout](run-storage-layout.md): the files a run writes.
- [How Evolution works](architecture.md): the campaign cycle and [how promotion is decided](architecture.md#how-promotion-is-decided).
- [Python API](api-usage.md): the task and skill API for embedding Core in a program.
- [Manual workflow](manual-workflow.md): the separate task and skill workflow with saved versions and traces.

Before upgrading this beta, read the [release notes](../CHANGELOG.md) and the [recovery guidance](running-campaigns.md#resume-after-a-failure).
