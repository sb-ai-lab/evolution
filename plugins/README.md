# Assistant plugins

Install Evolution's ready-to-use skills in the assistant you already use in your repository. Build the bundle from the Evolution source with Python 3.11 or later.

## Build and install

Run the builder from the Evolution repository. Set `package` for your assistant and keep the absolute bundle path it prints for the install command.

| Client | Set `package` to |
|---|---|
| [Qwen Code](#qwen-code) | `qwen` |
| [GigaCode CLI](#gigacode-cli) | `gigacode` |
| [Codex](#codex) or [Hermes Agent](#hermes-agent) | `agent-plugins` |

```sh
package=qwen
bundle="$PWD/dist/$package/evolution"
python3 "plugins/$package/build.py" "$bundle"
```

Install the bundle using your client's instructions below, then open a fresh assistant session in your own repository. You can ask, “Use the Evolution skills to help me set up a first evaluation for this repository.” The skills are available as soon as the plugin loads. To run an evaluation or campaign, the assistant uses an installed Lab or Core.

## Qwen Code

From the checkout where you want to use the plugin, run the install command with the absolute `bundle` path from the build step.

```sh
qwen extensions install "$bundle" --scope project
qwen extensions list
```

Start Qwen Code in that checkout. `/skills` lists the skills, and `/evolution:help` helps you choose a workflow. [Qwen Code extensions](https://github.com/QwenLM/qwen-code/blob/main/docs/users/extension/getting-started-extensions.md)

## GigaCode CLI

From your checkout, link the built extension using its absolute path. Then open GigaCode and check `/extensions`, `/skills` and `/evolution:help`.

```sh
gigacode extensions link "$bundle"
```

If `extensions link` is unavailable, copy the bundle's `skills/`, `commands/` and `references/` into the checkout's `.gigacode/` directory. Keep them together so links between skills and references resolve.

## Agent Plugins v1 for Codex and Hermes Agent

Both clients install the same `agent-plugins` bundle. [Agent Plugins v1](https://agent-plugins.org/specification)

### Codex

With `package=agent-plugins`, run this from the Evolution repository after building the bundle. [Build Codex plugins](https://developers.openai.com/plugins/build/plugins)

```sh
marketplace="$(dirname "$bundle")"
mkdir -p "$marketplace/.agents/plugins"
cp plugins/agent-plugins/marketplace.json "$marketplace/.agents/plugins/marketplace.json"
codex plugin marketplace add "$marketplace"
codex plugin add evolution@evolution-local
```

### Hermes Agent

Use a Hermes version that supports portable plugins. Activate the profile you want to use and run `hermes profile` to find its absolute path. Set `hermes_home` to the printed path, expand any leading `~/`, then copy and enable the bundle in that same profile. [Portable plugins in Hermes](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/#portable-agent-plugins-v1-packages)

```sh
hermes_home="/absolute/path/to/hermes/profile"
hermes_plugins="$hermes_home/plugins"
mkdir -p "$hermes_plugins"
mkdir "$hermes_plugins/evolution" && cp -R "$bundle/." "$hermes_plugins/evolution/" &&
  HERMES_HOME="$hermes_home" hermes plugins enable evolution
HERMES_HOME="$hermes_home" hermes plugins list --plain --no-bundled
```

If Hermes asks whether the plugin can override built-in tools, answer No.

## Connect a plugin to Lab

If you want the assistant to call a local Lab service over MCP from your own repository, build the plugin with the interpreter and socket for that service. Use `just setup` in `lab/` or install the Lab wheel with `[service,mcp]`. [Install Lab](../docs/guide/install.md)

Set `package` as shown above and replace the sample paths with your own. Both paths must be absolute. The `health` command checks the selected service if it is already running. You can build the configured plugin before the service starts.

```sh
lab_python="/absolute/path/to/lab/venv/bin/python"
lab_socket="/absolute/path/to/lab/service.sock"
"$lab_python" -E -P -m evolution_lab.cli --socket "$lab_socket" --json health
```

```sh
bundle="$PWD/dist/$package-mcp/evolution"
python3 "plugins/$package/build.py" "$bundle" \
  --lab-python "$lab_python" --lab-socket "$lab_socket" --autonomy ask
```

The builder checks that the interpreter has the same Lab version as this checkout and includes the MCP extra. Use paths without dollar signs or control characters. Qwen and GigaCode paths also exclude `{{args}}`, `!{` and `@{`.

| Mode | What happens before a start, resume or other action that requires approval |
|---|---|
| `ask` (default) | Lab shows a form if the client supports forms. Otherwise the client's tool approval rules apply. For CLI fallback, the assistant asks in the conversation. |
| `yolo` | Lab skips its form. The client's own approval rules still apply. |

Install the configured bundle using the commands above, with these client settings.

- **Qwen Code.** The extension declares the server in `qwen-extension.json`. Open a fresh session and check `/mcp`.
- **GigaCode CLI.** Copy the bundle's `mcpServers.evolution-lab-plugin` entry from `mcp-settings.json` into the repository's `.gigacode/settings.json`. Keep the other settings. If the entry already exists, compare the interpreter and arguments before replacing it. Restart GigaCode and check `/mcp`. [GigaCode project MCP configuration](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/mcp)
- **Codex.** Prefix both install commands and the command that starts Codex with the same dedicated setting, for example `CODEX_HOME="$HOME/.codex-evolution-lab"`.
- **Hermes Agent.** Use a separate profile and its path for `hermes_home`. Preserve the executable bit on `bin/lab-mcp` when copying the bundle.

To work with a registered Lab project, give the assistant its name. It can look up the ID with `projects_list`. Qwen Code and GigaCode CLI also offer `/evolution:status OPERATION` and `/evolution:report OPERATION` for saved operations. [Watch a campaign](../docs/guide/watch-campaign.md#the-commands)

## Check the connection

Open the intended checkout in a fresh session. For a configured package, look for one Lab server named `evolution-lab-plugin`. Some clients add a prefix to that name. Ask the assistant to call its `health` tool and compare the Lab version with the one you built against. If the client displays the server instructions, check the socket there too.

If a session shows both `evolution-lab` and `evolution-lab-plugin`, close it and use the profile for the connection you want. Use the workspace's server when working in a generated Lab workspace. The configured package's CLI instructions use the same interpreter, socket and autonomy as its MCP registration.

To change the interpreter, socket or mode, build into a new directory, replace the configured package and restart the client. For GigaCode, update the project's `evolution-lab-plugin` entry from the new bundle. To disconnect, remove the configured package or the GigaCode entry you added. [MCP guide](../docs/guide/mcp.md)

## Client checks

| Client | What was checked |
|---|---|
| Qwen Code 0.28.4 | The native extension installed in a test project and connected to the selected Lab. |
| Codex CLI 0.171.0 | The portable registration loaded and its launch configuration reached the selected Lab. A session with both a workspace and the configured plugin showed both entries. |
| Hermes Agent 0.20.0 | The portable registration loaded and its launch configuration reached the selected Lab. A profile with the configured plugin still loaded its entry in the workspace directory. |
| GigaCode CLI | The settings fragment worked against Lab over stdio. The CLI was unavailable on the test host. |
