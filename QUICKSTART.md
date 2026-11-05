# Your first Evolution campaign

This guide takes you from a checkout to a completed campaign and its selected files. It uses the packaged example: no API key, external agent or downloaded dataset is needed. The example makes a predictable edit so you can learn the workflow before connecting a real agent.

Commands below use a POSIX shell and Python 3.11.3 or newer. You need two terminals: one for the Target Service and one for the campaign. Run both from the same directory and activate the same Python environment.

## 1. Install the beta

From the repository root on `main`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[tree]"
evo --version
```

For this checkout, the last command should print `evo 2.0.0b16`. The `[tree]` extra installs the dependencies used by tree campaigns. Installation may download dependencies; running the example makes no LLM calls.

If your team gives you a wheel instead, create and activate a fresh environment, then install that exact file:

```bash
python -m pip install "/absolute/path/to/evo_skills-2.0.0b16-py3-none-any.whl[tree]"
evo --version
```

Replace the path with the file you received. Do not assume this beta has been published to PyPI. With a wheel installation, use an empty working directory for the following steps.

## 2. Create the example

```bash
evo-target init ./demo
```

This creates:

```text
demo/
├── manifest.json         # all campaign settings
├── seed/
│   ├── skills/SKILL.md   # the starting instructions
│   └── uv.lock           # a dependency file included in the tree identity
├── corpus.jsonl          # two evaluation cases
├── scorer/score.py       # gives each answer a score and verdict
├── reflector/reflect.py # points the editor at the failing behavior
└── editor/edit.py        # writes the example improvement
```

`manifest.json` is an `evolution-v2-manifest/15` document, the current manifest format, with its recommended settings. The [manifest reference](docs/manifest-v2.md#stable-family-keys-manifest-15) explains them.

`init` refuses a non-empty destination. If `demo` already exists, choose another name and use it in the following commands. Do not delete an existing run just to repeat the tutorial.

## 3. Start the Target Service

In terminal 1:

```bash
evo-target serve ./demo/manifest.json
```

Wait for a JSON line containing `"status": "listening"`. Leave this terminal running. The default address is `http://127.0.0.1:8765`.

This service is the example agent. For each message, it replies with the first line of the loaded `skills/SKILL.md`, followed by the message. It does not call a model. In a real integration, your own backend runs the agent in its place.

## 4. Check the connection and run the campaign

In terminal 2, activate the same environment and return to the same working directory:

```bash
source .venv/bin/activate
evo tree conformance --manifest ./demo/manifest.json
```

Conformance checks that the service follows the target protocol, including loading the requested files and handling sessions correctly. Its JSON report should contain `"ok": true` and an empty `leaked` list. It checks the integration, not the quality of the agent's answers.

Now run the campaign:

```bash
evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1 \
  > ./demo/result.json
```

The campaign writes one JSON result to stdout, saved here as `demo/result.json`. Progress and continuation instructions appear on stderr. The shell creates this result file; Evolution stores the campaign's own records under `demo/run-1`.

## 5. Read the result

```bash
python - <<'PY'
import json
from pathlib import Path

result = json.loads(Path("demo/result.json").read_text())
print("State:", result["terminal"])
print("Outcome:", result["finished"])
print("Champion:", result["status"]["final_champion"])
print("Files:", result["status"]["campaign_outcome"]["champion_path"])
PY
```

For the unmodified example, expect:

- `terminal` is `finished`, with outcome `completed` and reason `rounds_exhausted`.
- The final Champion is `v1`, stored at `demo/run-1/artifacts/champions/v1`.
- The first round promotes the edit. The second finds no remaining failing cases and closes cleanly.

Compare the files:

```bash
cat ./demo/seed/skills/SKILL.md
cat ./demo/run-1/artifacts/champions/v1/skills/SKILL.md
```

The selected copy starts with `IMPROVED`; the original seed does not change. The sample scorer explicitly rewards this marker, so this proves that the wiring works. It is not evidence that the sample skill would help a real agent.

You may see a `continue:` message after success. It explains how to start a successor campaign; it is not an error and requires no action to finish this example.

## 6. Stop or repeat the example

Press Ctrl+C in terminal 1 to stop the service when you are done. To run a separate experiment with the same example, start the service again and use a new directory such as `./demo/run-2`.

Reusing `./demo/run-1` reads the existing campaign. It does not erase the run or create extra rounds in a campaign that has already finished. For a longer experiment that continues from its Champion, follow [Continue a completed campaign](docs/running-campaigns.md#continue-a-completed-campaign).

## If something goes wrong

If a run stopped and you do not know why, start with [A run failed: start here](docs/running-campaigns.md#a-run-failed-start-here) for a status command, log locations and what to share when asking for help.

| What you see | What to do |
|---|---|
| `evo` or `evo-target` is not found | Activate the environment where you installed the package. |
| `cannot bind` | Port 8765 may be in use. Change `target.base_url` in `demo/manifest.json` before the first run, then restart the service. |
| Connection refused or a failed health check | Check that terminal 1 is still running and uses the same manifest. |
| `scorer_invalid at scorer.argv` | The example launches `python3`. Make sure it is on the activated environment's `PATH`. |
| `refused: ...` | Read stderr for the invalid setting or changed input. See [Refusals](docs/running-campaigns.md#handle-a-refusal). |
| `suspended: ...` | Repair the named cause, then rerun the same `evolve` command. See [Resume after a failure](docs/running-campaigns.md#resume-after-a-failure). |

## Next steps

- [How Evolution works](docs/architecture.md) explains the terms and the campaign cycle.
- [Running campaigns](docs/running-campaigns.md) covers your own cases, results and recovery.
- [Connecting an agent](docs/connecting-an-agent.md) takes you from the example target to your agent.
- [Contributing](CONTRIBUTING.md) explains how to work on Evolution itself.

For the separate task/skill workflow that starts with `evo quickstart -m mock` and creates `hello-evolution` and `hello-skill`, use the [manual workflow guide](docs/manual-workflow.md).
