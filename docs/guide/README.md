# Evolution Lab user guide

[Русский](ru/README.md)

Evolution Lab helps you measure an LLM agent honestly and improve it with the
Evolution Core: it freezes your inputs, runs your agent as a service,
measures it, and shows what changed — without ever passing off an
infrastructure error as agent quality.

## Work with a coding assistant

Lab ships ready-to-use skills that guide your assistant through connecting an agent, setting up an evaluation, running a campaign and reading the results. [Build and install the plugin for your own repository](../../plugins/README.md#build-and-install).

## Before you start

This guide is for **developers who are new to Lab**. You should know Python and
have an agent you care about; you do not need to know anything about Lab or
the Evolution Core yet. Pages explain why before how, define every term at
first use. Your own agent starts from the **project skeleton**:
`lab new-project DIR` writes a small project with a stub agent, an adapter, a
corpus and a scorer, and `lab evaluate DIR` measures it (new in
0.0.3.dev72). The demo and the runnable examples use the same skeleton. A
small **order-lookup assistant** with a single tool, `lookup_order`,
illustrates the parts a real agent adds: settings, tools and failures.

Current version pair: `evolution-lab 0.0.3.dev81` with exactly
`evo-skills 2.0.0b22` (released as tag `lab-v0.0.3.dev81`; wheel checksums and
source provenance in `docs/RELEASES.md`). Linux or macOS, Python ≥ 3.11.4, built
from source (not on PyPI yet). Other pairs: [RELEASES](../../lab/docs/RELEASES.md).

On macOS, a process that leaves the group, escapes the tree between polls and hides the environment marker can survive a stop. See [Install](install.md#macos-notes) for native macOS setup.

## Pick your path

Everyone starts with [How Lab works](concepts.md) and [Install](install.md),
then runs [the demo](demo.md) to see Lab measure and improve a synthetic agent.
To see a complete minimal agent wired into Lab before writing your own, read
[A tour of the examples](examples.md). Then follow the path that matches your
goal.

### "I want to measure my agent"

You want a trustworthy score for an agent as it is today — a baseline.

1. [Connect your agent](connect-agent.md) — `lab new-project`, then what to
   edit in each file
2. [Run your first evaluation](first-evaluation.md) — `lab evaluate`, its run
   files and exit codes
3. [Evaluation inputs](evaluation-inputs.md) — real cases and a real scorer
4. [Read the results](results.md)
5. [Pitfalls](pitfalls.md)

### "I want to improve my agent"

You want the Evolution Core to propose changes and keep only those that
measurably help. The fastest way to see the whole loop is the free demo
project — no model, no network, no git — before you spend anything on your
own agent: [Projects and scenarios](projects.md) walks it through `connect`
and one `evolve` cycle. Then do the measuring path with your own agent: a
campaign needs the same connected agent and evaluation inputs, and you want
a baseline to compare against.

1. [Projects and scenarios](projects.md) — the demo project, free
2. Everything in "I want to measure my agent"
3. [Run a campaign](campaign.md)
4. [Budget and cost control](budget.md)
5. [Read the results](results.md) — diff and export of the final version

### "I'm integrating Lab into my own tool or service"

You are building a program that prepares experiments and drives Lab, the way
the auto-evolve app does.

1. [CLI and local service](cli.md) — the Unix-socket service and its commands
2. [Evaluation inputs](evaluation-inputs.md) and [Run a campaign](campaign.md)
   — what a package must contain
3. [Read the results](results.md)
4. The requirements catalogue: `lab guides requirements --json` lists every
   item a package must settle, with a digest you can pin

## All pages

| Page | What you get |
|---|---|
| [How Lab works](concepts.md) | The ideas: Target Service, cases, scorer, fixed evaluation vs campaign, Bundle, Owner, version pair. |
| [Install](install.md) | Build both wheels, install them, verify, and run a complete offline example. |
| [The demo](demo.md) | What `just demo` and `just demo-campaign` do, what they print and which files to open. |
| [A tour of the examples](examples.md) | What the offline examples consist of, which file plays which role, and the order to read them in. |
| [Connect your agent](connect-agent.md) | Start from the project skeleton and turn your agent into a target: the adapter, outcomes and the launch file. |
| [Evaluation inputs](evaluation-inputs.md) | Write the corpus, environment, scorer and metric. |
| [Run your first evaluation](first-evaluation.md) | Measure your agent with `lab evaluate`: the readout, run files and exit codes; then the same flow with the Python API. |
| [Run a campaign](campaign.md) | Let Core improve your agent: manifest, editable scope, Editor and Reflector. |
| [Read the results](results.md) | Status, reports, coverage, diffs, exports, and what each fact does and does not mean. |
| [CLI and local service](cli.md) | The same actions through `lab serve` and the `lab` command. |
| [Watch a campaign](watch-campaign.md) | Follow a running campaign with `lab watch`, read its lines, and act when it pauses or ends abnormally. |
| [Projects and scenarios](projects.md) | Register an agent as a Lab project and walk the `connect`, `evolve` and `restructure` scenarios, starting from the free demo; dev70 also adds `create-skill`. |
| [The Lab panel](panel.md) | Open the read-only web page over the service: health, operation list, live campaign progress. |
| [Connect an assistant over MCP](mcp.md) | Let an MCP client read what the service knows through `lab mcp`: tools, guides, errors. |
| [Budget and cost control](budget.md) | What Lab does and does not do about LLM spend, and the budget gateway. |
| [Pitfalls](pitfalls.md) | Traps that silently spoil a measurement, and how to avoid them. |

## Where the exact references live

This guide teaches; it does not list every field and error code. When you need
the precise contract, go to:

- **Guides shipped inside the wheel** —
  [`src/evolution_lab/guides/`](../../lab/src/evolution_lab/guides/). They are
  written as compact specifications (partly for AI assistants), and the copy
  inside your installed wheel always matches your installed version.
  `lab guides list` and `lab guides path` find them.
- **API references** — [`docs/api/`](../../lab/docs/api/): preparation, Owner, service,
  budget gateway, legacy runner.
- **Examples** — [`examples/`](../../lab/examples/README.md);
  `examples/owner/evaluation.py` is the one to start with, and
  [A tour of the examples](examples.md) explains its files. Most other examples
  are tied to older version pairs and kept for reading.

## Reference

For exact fields and error codes:

- [Guides in the wheel](../../lab/src/evolution_lab/guides/) — start with the [requirements guide](../../lab/src/evolution_lab/guides/requirements/SKILL.md)
- [API references](../../lab/docs/api/) — including the [panel](../../lab/docs/api/panel.md)
