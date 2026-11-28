# Quickstart

[English](QUICKSTART.md) · [Russian](QUICKSTART.ru.md)

This guide takes you from a clone to a measurement of your own agent and then to a campaign that tries to improve it, all through Evolution Lab. The first two steps need no API key, model calls or network access after setup. To drive Core without Lab, use the [Core quickstart](docs/core-quickstart.md) instead.

You need Linux or macOS, Python 3.12 or 3.11 (3.11.4 or newer), [uv](https://docs.astral.sh/uv/), [just](https://just.systems/), git and read access to this repository. The [installation guide](docs/guide/install.md) explains each requirement.

On macOS, a process that leaves the group, escapes the tree between polls and hides the environment marker can survive a stop.

## 1. Set up

```sh
git clone https://github.com/sb-ai-lab/evolution.git
cd evolution/lab
just setup
just lab guides list
just lab scenarios list
```

`just setup` builds the Core wheel from this repository, keyed by the last commit that changed anything outside `lab/`, builds the Lab wheel from this checkout and installs both into `.lab/venv`. Run it again after every `git pull`. In this checkout, `just lab COMMAND` runs `lab` from that environment. If setup stops, see [Install → Troubleshooting](docs/guide/install.md#troubleshooting).

The two lists work offline and show where to find the skills shipped with Lab.

- `just lab guides list` lists the reference skills for connecting an agent, preparing an evaluation or campaign, and reading results. The file paths are relative to the directory printed by `just lab guides path`.
- `just lab scenarios list` shows the number of steps and the skill file for each project scenario. The scenarios are `connect`, `create-skill`, `evolve` and `restructure`.

To use the skills in your own repository, [build and install the plugin](plugins/README.md#build-and-install) for Qwen Code, GigaCode CLI, Codex or Hermes Agent. For a project already registered with Lab, a [generated assistant workspace](docs/guide/projects.md#step-8-work-with-an-assistant) includes the skills and can connect to Lab over MCP.

By default, Lab and Core survive process crashes without per-record fsync; a host crash or power loss can lose recent work. Set `EVOLUTION_DURABILITY=strict` before launching Lab to restore the previous durability. SQLite uses WAL+NORMAL by default, so `-wal` and `-shm` files beside the databases are expected. Lab sets the effective mode in its workers. See [durability](docs/guide/cli.md#durability).

## 2. Watch the offline demos

```sh
just demo
just demo-campaign
```

`just demo` measures a stub agent with a complete fixed evaluation: it freezes the inputs into a Bundle, checks that the agent speaks the protocol, scores three questions and prints each answer with its score. `just demo-campaign` lets Core improve a demo agent: the score goes from 0 to 1, and `changes.diff` shows the line the Editor rewrote. [The demo](docs/guide/demo.md) explains both and which files to open.

## 3. Measure your own agent

```sh
just new-project ~/my-agent-lab
just evaluate ~/my-agent-lab
```

`just new-project DIR` writes a project skeleton: a stub agent (`agent/my_agent.py`), the adapter that connects it to Lab (`agent/adapter.py`), three questions in `corpus.jsonl` and a scorer. Run `just evaluate DIR` once on the untouched skeleton to see a successful run, then replace the agent, the questions and the scorer. [Connect your agent](docs/guide/connect-agent.md) says what to edit in each file, and [Run your first evaluation](docs/guide/first-evaluation.md) explains the output, the run files and the exit codes.

## 4. Run a campaign

```sh
just campaign ~/my-agent-lab
```

A campaign repeats edit, measure and compare rounds under Core's rules and keeps the best version. The skeleton runs offline with sample roles. Replace them for your agent; model-driven roles incur costs. The campaign uses `manifest.json` and `campaign-launch.json` from the same project. [Run a campaign](docs/guide/campaign.md) walks from the offline example to your own manifest, editable scope, Editor, cost cap and outcome. [Budget and cost control](docs/guide/budget.md) explains how Lab caps LLM spending.

## Next steps

- [The user guide](docs/guide/README.md) has all pages in order, with a path for measuring, improving or integrating Lab into another tool.
- [Read the results](docs/guide/results.md): the report, the diff and the export.
- [Evolution Core](docs/core.md): Core's own commands, for direct and embedded use.
