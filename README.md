![Evolution — the film’s ring-and-dot mark connected by branching paths.](docs/assets/readme/evolution-header.png)

# Investigate. Edit. Compare.

[English](README.md) · [Russian](README.ru.md)

Turn your agent’s failures into proposed changes to its skills and prompts. Evolution evaluates each change against your cases before selecting a version.

**[Try it without an API key →](#quickstart)** · [Connect your agent](docs/connecting-an-agent.md) · [How it works](docs/architecture.md)

![Illustrative workflow: investigate an ungrounded answer, edit a skill, reject one candidate, select another, then use the selected instructions to query data and answer the same task.](docs/assets/readme/evolution-film.gif)

[View the static example](docs/assets/readme/evolution-film-poster.png).

## A better instruction starts with a specific failure.

The agent gives general retention advice without checking product data. Investigation identifies the missing query. The proposed skill now says to query the data and compare retention cohorts. Evolution tests the candidate; your checks decide whether it is selected.

You supply the agent, editable files, cases and scorer. Evolution preserves the selected files and the run history. A campaign can keep the current version.

Python 3.11.3+ · Beta `2.0.0b18` · [Apache 2.0](LICENSE)


## Quickstart

Run a complete example without an API key. It uses a deterministic agent and scripted roles, so it verifies the wiring rather than real-agent quality. Use Python 3.11.3+ and Bash or Zsh.

### 1. Install this beta

```bash
git clone https://github.com/sb-ai-lab/evolution.git
cd evolution
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[tree]"
evo --version
```

Expect `evo 2.0.0b18`. Install from the documented beta checkout or a supplied wheel; a plain PyPI install may resolve to another release. If your checkout reports another version, follow that version’s documentation. [Wheel installation](QUICKSTART.md#1-install-the-beta) is covered in the full guide.

### 2. Start the example service

In terminal 1, from the same `evolution` directory:

```bash
evo-target init ./demo
evo-target serve ./demo/manifest.json
```

Wait for a JSON line containing `"status": "listening"` and leave the service running. If port 8765 is busy, stop this service, change `target.base_url` in `demo/manifest.json` to an available loopback port, and restart it before the first campaign. If `demo` already exists, use a new directory name throughout these commands.

### 3. Run the campaign

Open terminal 2 in the same `evolution` directory and activate the same environment:

```bash
source .venv/bin/activate
evo tree conformance --manifest ./demo/manifest.json
evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1 \
  > ./demo/result.json
```

Conformance should report `"ok": true` and `"leaked": []`. The campaign saves its result JSON in `demo/result.json` and its records in `demo/run-1`.

### 4. Inspect the selected files

```bash
python - <<'PYRESULT'
import json
from pathlib import Path

result = json.loads(Path("demo/result.json").read_text())
print("State:", result["terminal"])
print("Outcome:", result["finished"])
print("Champion:", result["status"]["final_champion"])
print("Files:", result["status"]["campaign_outcome"]["champion_path"])
PYRESULT
cat ./demo/seed/skills/SKILL.md
cat ./demo/run-1/artifacts/champions/v1/skills/SKILL.md
```

Expect a finished campaign with outcome `completed`, reason `rounds_exhausted` and **Champion `v1`**. The selected skill begins with `IMPROVED`; the original seed does not. The sample scorer rewards that marker, so this result demonstrates file flow and selection. It does not establish improvement for a real agent.

Stop the service with Ctrl+C in terminal 1 when finished. A `continue:` hint after success is optional guidance for a successor campaign. To repeat the example, start the service and use a new run directory.

The [full Quickstart](QUICKSTART.md) explains generated files and troubleshooting. For an unfamiliar failure, start with [diagnosis and recovery](docs/running-campaigns.md#a-run-failed-start-here).

## What happens between an edit and selection?

The Grouper gathers observed failures; the Reflector investigates a group; the editor changes a copy of permitted files. Preliminary checks compare the Candidate on affected training cases. A finalist then faces fresh measurement across training cases and comparison with the current Champion. A campaign can finish without a promotion. [Read the comparison rules](docs/architecture.md#how-promotion-is-decided).

Evolution changes instruction files, without training model weights. Training evidence guides edits and selection; held-out cases measure the fixed selected version separately.

## Bring your own agent

Your agent needs a **Target Service** wrapper that loads each supplied file snapshot and returns the agent’s behavior. Its model-provider URL is a separate connection. The [integration guide](docs/connecting-an-agent.md) and [complete offline backend](docs/target-server.md#a-complete-offline-backend) show what to implement.

## Keep going

- [Run and inspect campaigns](docs/running-campaigns.md), including separate [held-out evaluation](docs/running-campaigns.md#improve-on-train-and-measure-on-test).
- [Diagnose a stopped run](docs/running-campaigns.md#a-run-failed-start-here).
- Look up [commands](docs/cli.md#tree-service-commands), [campaign settings](docs/manifest-v2.md) or the [backend API](docs/target-server.md).
- [Browse all guides](docs/SUMMARY.md), [contribute](CONTRIBUTING.md) or [report an issue](https://github.com/sb-ai-lab/evolution/issues).

The separate [task and skill workflow](docs/manual-workflow.md) uses saved versions and traces. Before upgrading this beta, read the [release notes](CHANGELOG.md) and [recovery guidance](docs/running-campaigns.md#resume-after-a-failure).
