# Evolution Lab

[Русский](README.ru.md)

This directory is the `evolution-lab` package: the recommended way to run
Evolution. Lab prepares a reproducible experiment to improve an LLM agent,
runs it on Core (`evo-skills`, the rest of this repository), measures it
honestly and lets you take the result home.

```text
evolution-lab 0.0.3.dev81  +  evo-skills 2.0.0b22 (exact)   ·   Python ≥ 3.11.4   ·   Linux or macOS
```

[Version pair and installation](docs/RELEASES.md).

**Using Lab?** Start with the repository's [README](../README.md) and
[Quickstart](../QUICKSTART.md). The [user guide](../docs/guide/README.md)
explains every task, and [How Lab works](../docs/guide/concepts.md) defines
the terms. This page is for working on the package itself.

On macOS, a process that leaves the group, escapes the tree between polls and hides the environment marker can survive a stop.

## Quick start

On Linux or macOS with `uv`, `just` and `git` (the full list is in
[Install](../docs/guide/install.md)):

```sh
git clone https://github.com/sb-ai-lab/evolution.git
cd evolution/lab
just setup                         # core wheel + Lab wheel into .lab/venv
just demo                          # an offline evaluation: no LLM, no keys
just new-project ~/my-agent-lab    # a project skeleton for your own agent
just evaluate ~/my-agent-lab       # its first measurement
```

`just setup` builds the Core wheel from the repository's committed files,
keyed by the last commit that changed anything outside `lab/`, builds the Lab
wheel from this checkout and installs both into `.lab/venv`. An editable
install (`pip install -e .`) does not work: Lab runs Core and your agent in
isolated `python -I` subprocesses and checks the installed files against the
wheels. `just` lists all recipes.


## Release

[Releases](docs/RELEASES.md) lists the versions in this snapshot.

## Layout

```text
src/evolution_lab/
  preparation/   Bundle and prepared execution
  owner/         Python Owner: campaign, evaluation, conformance, results
  target/        TurnAgent turn protocol and target launcher
  service/       local Unix-socket service ([service] extra)
  cli/           the `lab` command
  panel/         optional browser panel
  guides/        bundled guides and catalogues for agents, shipped in the wheel
  observations/  local trace import
  projects/      Lab home, project registry, demo project
  quickstart/    project skeleton, lab new-project and lab evaluate
  client.py      single-campaign client
  budget.py      budget gateway ([gateway] extra)
  runner.py, recipe.py, report.py, evaluation.py, standalone.py   legacy path
tests/           pytest
examples/        agent-neutral examples, see examples/README.md
docs/            API reference
                 (user guides are in ../docs/guide/)
```

- [API reference](docs/api/): exact shapes, states and error codes.
