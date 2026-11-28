# Connect your coding assistant to Lab over MCP

[Русский](ru/mcp.md)

To use the ready-to-use Evolution skills in your own checkout, [build and install the plugin](../../plugins/README.md#build-and-install). This page covers access to the local Lab service. For your own checkout, [connect the plugin to Lab](#opt-in-plugin-setup). For other MCP clients, see [manual setup](#start-it-from-your-client).

## In a Lab project workspace

If you want Lab to open a dedicated workspace for a registered project, follow these steps. They work with Claude Code, Codex, Qwen Code and GigaCode CLI.

1. In the Evolution repository, run `just setup` from `lab/` if you still need to [install Lab](install.md). Start the local service with `just serve` and leave it running.

   If you installed from wheels, [start the service with `lab serve`](projects.md#before-you-start) in an environment with `[service,mcp]` installed. Use `lab` instead of `just lab` in the following steps.

2. In a second terminal in `lab/`, find your project ID.

   ```sh
   just lab --json project list
   ```

   To try the setup with an offline demo, run `just lab project demo demo` and use `demo` as the ID.

3. Open the project, replacing `ID` with your project ID.

   ```sh
   just lab project open ID --autonomy ask
   ```

4. Start your assistant in the directory printed by the command. If prompted, approve the `evolution-lab` server or trust the workspace. Ask the assistant to call `health` and `guides_list`. You should see the Lab version and a list of installed guides.

### Workspace connection details

The client uses the Python interpreter and socket of the Lab service that opened the workspace. Lab dev79 adds MCP tools to prepare inputs and start or resume campaigns. Use `operation_start` to check that the client can see the new tools.

The workspace writes these client files when the service environment has the MCP extra:

| File | Client |
|---|---|
| `.mcp.json` | Claude Code |
| `.codex/config.toml` | Codex |
| `.qwen/settings.json` | Qwen Code |
| `.gigacode/settings.json` | GigaCode CLI |

Each declares the server `evolution-lab`. Its command is the interpreter of
the service that wrote the workspace, and its arguments are
`-E -P -m evolution_lab.cli mcp --socket SOCKET --autonomy LEVEL`: the socket is explicit, so
the working directory and `LAB_HOME` do not matter. The files are the same
bytes for the same service, project, installation and autonomy level. Lab writes nothing
outside the workspace and no trust or approval settings.

- **Approval.** The client's own approval rules apply, and Lab never approves
  or trusts the server for you. Interactive Claude Code, Qwen Code and GigaCode
  CLI ask you once to approve the project server; non-interactive modes such as
  `claude -p` load project servers without asking. When asked, approve it, then check that the tools answer,
  for example `claude mcp list` shows `evolution-lab` and the assistant's
  `project_status` tool returns the project's status.
  GigaCode CLI is built on Qwen Code, so Lab treats its file location
  (`.gigacode/settings.json`) and its approval behaviour like Qwen Code's. That is
  an assumption until a live GigaCode session confirms it; if the server does not
  appear there, say so.
- **Codex.** Codex reads a project `.codex/config.toml` only in a directory
  you trust. It asks when you start it there; or add a trust entry for the
  workspace to your own `~/.codex/config.toml`. Lab never writes that entry.
  Codex CLI 0.171.0 loaded the entry in a trusted Lab workspace without a Git
  repository during the offline approval check. If the server does not appear,
  use the CLI commands in the workspace's `AGENTS.md`.
- **Without the extra.** If `mcp` is not installed in the Lab extra's range (`>=2.3,<3`) where `lab serve` runs,
  none of the four files is written, and `AGENTS.md` says "Lab tools are not
  configured". In a Lab checkout run `just setup` again; for a wheel install
  reinstall the Lab wheel with `[service,mcp]` in the environment of
  `lab serve`; then run `lab project open ID` again.
- **After a reopen or an upgrade.** The running server keeps what it read at
  startup. After `lab project open` regenerates the workspace, or the service
  on this socket is restarted or upgraded, restart the assistant session in
  the workspace, or reconnect the `evolution-lab` server where the client
  offers it. After you upgrade or move Lab, or start the service on another
  socket, run `lab project open ID` again with your chosen `--autonomy`.

The workspace `AGENTS.md` records the selected level. Its CLI commands use the
service's absolute interpreter path with `-E -P -m evolution_lab.cli` and an explicit `--socket`.

Limits of the generated configuration:

- The workspace configures the server only when `mcp` is a final release inside
  `>=2.3,<3`; a pre-release, dev or local build counts as absent, and `AGENTS.md`
  carries the "not configured" notice.
- Python reads `PYTHONUSERBASE` even with `-E`. If Lab is installed with
  `pip install --user` under a custom `PYTHONUSERBASE` that the assistant client
  does not have, the server cannot start. The CLI fallback in `AGENTS.md` uses
  the same interpreter and fails the same way, so both need an environment where
  Lab is importable: the same `PYTHONUSERBASE` in the client, or a venv install.
- Claude Code expands `${VAR}` in `.mcp.json`. If the server command or any
  argument contains `$`, Lab does not write `.mcp.json`; the other three files
  are written, and `AGENTS.md` says Claude Code is not configured and points to
  the CLI fallback.

## Opt-in plugin setup

In your own checkout, use a configured Qwen Code, GigaCode CLI or Agent Plugins v1
package. Choose the absolute Python path from your Lab environment, the service's
absolute socket path, and `ask` or `yolo`. The package includes the same connection
and autonomy in its Lab CLI instructions.

After installation, ask the assistant to call `health` on `evolution-lab-plugin`.
In a generated Lab workspace use its `evolution-lab` registration. Keep a configured
portable plugin in a separate Codex home or Hermes profile. [Build and connect a plugin](../../plugins/README.md#connect-a-plugin-to-lab).

## Autonomy and action approval

- **`ask` (default).** Lab asks before a costly or irreversible action or a decision on the human's behalf.
  If the client declares form support, Lab presents a form. Otherwise the client's tool approval rules
  apply. With the CLI, the assistant asks in the conversation and runs the command after a yes.
- **`yolo`.** Lab runs the requested action without a form. The client's own approval rules still apply.

To change levels, run `lab project open ID --autonomy ask` or `lab project open ID --autonomy yolo`
again, then restart or reconnect the server. For a configured plugin, rebuild with the chosen `--autonomy` and reconnect.
For a manual connection, use `lab mcp --autonomy`.

The form shows the action, the object and the state being approved. For a start it shows expected
calls when Lab can derive them, the gateway cap or `no Lab spending limit`, and allowed automatic
resumes. The gateway caps only routed calls. A decline, cancellation or expiry of
`--approval-timeout SECONDS` (default 600) leaves the action unapplied. The service checks the shown
state when admitting the action.

Through MCP `2025-11-25`, the form appears during the call. From `2026-07-28`, the client sends the
answer on its next call. Lab shows an updated form if the facts changed.

A client can ask for permission to invoke the tool and then show the Lab form. We tested both
clients with an offline campaign start at `ask`.

- Claude Code 2.1.331 showed the form in the terminal. Declining left the campaign unstarted.
  Confirming started it. In `claude -p`, the client refused the call.
- Codex CLI 0.171.0 showed the form in an interactive session with `on-request`, including when
  auto-review allowed the tool call. In both modes, the campaign started after the human
  confirmed. With auto-review, declining left the campaign unstarted. In `codex exec`, the client
  blocked the tool call, or the form was cancelled after auto-review allowed the call.

Lab follows the capabilities the client declares. Qwen Code and GigaCode without forms use their
own tool approval rules, so their automatic modes can call a tool without asking the human.
Approval records use `form` for an accepted form, `client_rules` for the client's tool rules,
and `none` at `yolo`. Read them with `lab approvals --operation OP` or `lab project approvals ID`.

Preparation, checks, conformance, target starts and stops go ahead without a Lab question. Checks and
conformance rely on the adapter to answer offline. An adapter that forwards them to a paid model
can incur spending without a Lab question. Provide secret values through `secrets_file`, the absolute
path to a JSON file. Forms display secret names only.

## Start it from your client

Use this to register the server by hand: for a client without a generated
file, or outside a workspace.

Register a stdio server whose command is `lab` and whose arguments are `["mcp"]`:

```json
{"command": "lab", "args": ["mcp"]}
```

In a checkout `lab` is not on the `PATH`; use the absolute path of the Lab
environment's script as the command:
`{"command": "/path/to/evolution/lab/.lab/venv/bin/lab", "args": ["mcp"]}`.

To pin the socket, use `{"command": "lab", "args": ["mcp", "--socket", "/path/to/service.sock"]}`.
`--home DIR` is the other way to choose it, and `--timeout SECONDS` bounds each
tool call (default 60): the exchange with the service and the server's own
waits. Decoding and checking a response already received may run briefly past
it. `--socket` and `--home` take absolute paths;
a relative one is refused.

## Which service it reads

The socket is chosen as for every adapter: `--socket`, else `--home`, else the
default Lab home (`LAB_HOME`, else `$XDG_DATA_HOME/evolution-lab`), with
`service.sock` inside. The working directory is never consulted: when
`LAB_HOME` or `XDG_DATA_HOME` is relative, `lab mcp` exits 2 with
`usage: socket path must be absolute`. The server instructions name the
resolved socket. `lab mcp` does not start the service:
start it with `lab serve --state DIR` first, then call `health`.

## Tools

An id that becomes part of the request path (project,
operation, measurement, case, sample, attempt, reflection, digest, target, task,
scenario, schema, guide name) must be non-empty. A filter or other query
argument (`implementation`, `scenario` of `project_status`, `path`, `bundle`,
`owner_generation`, `tree_from`, `tree_to`, the guide `file`) is passed on as
given, an empty string included, and the service (or the guide reader) answers
it as it answers the CLI. Ids come from the list tools, and each argument's
description says which.

| Group | Tools |
|---|---|
| Service | `health`, `scenarios_list`, `scenario_show`, `scenario_schema`, `requirements` |
| Operations | `operations_list`, `operations_summary`, `operation_status`, `operation_failure`, `operation_refusal`, `operation_inputs`, `operation_routing`, `operation_suspensions` |
| Reports | `operation_report`, `operation_report_markdown`, `operation_campaign` |
| Measurements and trees | `operation_measurements`, `operation_measurement`, `operation_sample`, `operation_attempt`, `operation_reflection`, `operation_diff` |
| Projects | `projects_list`, `project_show`, `project_status`, `project_facts`, `project_confirmations`, `project_tree`, `project_operations`, `project_operations_summary` |
| Bundles and policies | `bundle_show`, `bundle_measurements`, `prepared_show`, `execution_policies_list`, `execution_policy_show` |
| Targets and tasks | `targets_list`, `target_status`, `task_status` |
| Guides | `guides_list`, `guide_read` |

`project_tree` takes `path` relative to the tree; use `.` for its root. The
guide tools and the guide resources work without the service.

| Actions | Tools |
|---|---|
| Preparation and targets | `bundle_create`, `prepare`, `connect`, `target_start`, `target_stop`, `check`, `conformance` |
| Operations and tasks | `operation_start`, `operation_resume`, `operation_stop`, `task_stop`, `delete_results`, `release`, `cleanup` |
| Projects | `project_bundle`, `project_prepare`, `project_target`, `project_start`, `project_sync_check`, `project_sync`, `project_confirm`, `project_accept_preview`, `project_accept_commit`, `project_revoke_confirmation`, `project_revoke_acceptance` |
| Policies and approvals | `policy_confirm`, `approvals_list`, `project_approvals` |

Mutating tools take `request_id` and the service's `owner_generation` (from `health`).
Repeat both keys and the original arguments on retry. If a restarted MCP server builds a different
request, the service can refuse it with `request_conflict` or a bound-state refusal.
After `acceptance_unknown`, read the current state before retrying.

## Guides as resources

The installed guides are also MCP resources: `lab://guides/<name>/SKILL.md`
for each guide, `lab://guides/scenarios/<id>/SKILL.md` for each scenario
guide, and the template `lab://guides/{name}/{file}` for the other files of a
guide. `lab://guides/<name>` reads its `SKILL.md`.

## Version notice

At startup `lab mcp` reads `health` once, within the smaller of `--timeout` and
5 seconds. When the service's `lab_version` differs from the installed
`evolution-lab`, the server instructions and every guide tool result say so:
the guides describe the installed version, not the service's. The instructions
cover the guide resources too; a resource read carries no notice of its own.
This read takes `lab_version` from any `health` answer, even one of another
schema version; the `health` tool itself still checks the schema. If the
startup read fails, the first guide call tries once more, for at most half
of its `--timeout` and 5 seconds; a read cut off adds no notice, and the
guide result comes without it.

## Errors

A failed call returns the CLI's envelope, `{"error": {"code", "stage", "message"}}`
with an optional `hint`, as structured content and as JSON text, with
`isError` set. Errors from the service pass through whole. The codes `lab mcp`
adds itself:

| Code | Stage | When |
|---|---|---|
| `request_timeout` | transport | The call did not finish within `--timeout`: the service request or a guide read. Retry. |
| `request_interrupted` | transport | The connection broke during the request. Retry. |
| `acceptance_unknown` | transport | A mutation's result was lost in transport. Read the current state before retrying. |
| `service_unavailable` | client | No service on the socket. The hint says to start it and names the socket. |
| `invalid_service_response` | client | The answer is not what the schema allows. The Lab service and `lab mcp` may be different versions. |
| `usage_error` | client | An argument is missing, unknown, of the wrong type or a malformed id. It is checked against the tool's input schema before any request; the id decoders then refuse a malformed id (`a/b`, a bad digest, an unknown guide or scenario). |
| `internal_error` | client | An unexpected failure inside `lab mcp`; the message is generic. |
| `approval_declined`, `approval_cancelled`, `approval_failed`, `approval_timeout` | approval | The form did not give valid consent. The action request was not sent. |
| `request_conflict` | approval | The key already identifies a mutation with different arguments in this session. |

## What it does not do

- No disclosed reads and no probe dialogs; those stay in the CLI.
- It does not start the service.
- stdio only: no HTTP or remote transport.
- No MCP prompts, sampling or URL-mode elicitation.


For the service and the other commands, see [CLI and local service](cli.md).
