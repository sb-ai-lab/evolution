---
name: evolution-lab-scenario-connect
description: Connect an existing agent to Lab step by step — spec, package, check, conformance and a short baseline — and know when each step is done.
---

# Scenario `connect`: connect an agent

To measure a project directory before registration, use [first evaluation](../../first-evaluation/SKILL.md).

The scenario ends when the implementation is **connected**: `check`, `conformance` and a
fixed-evaluation baseline hold on Bundles with the same inputs. This skill says what to
do in Lab at each step and which guide explains the contract. How to analyse an agent and
write its spec is not part of it.

Before you start:

- The project and its implementation exist: `lab project show <project>`.
- Project commands below name the implementation with `--implementation <implementation>`;
  while the project has one active implementation the flag may be omitted.
- Service commands talk to a running `lab serve --home <home>`. Put the global option
  `--socket <path>` before the command name when the service is not found by default.
- `lab project status` shows each step as `done`, `stale`, `ready` or `blocked` with
  what is `missing`. Start there and return to it after every step:

```bash
lab project status <project> --implementation <implementation> --scenario connect
```

- Guides are named by skill name: `evolution-lab-<name>`, with an anchor after `#`. With
  only the CLI, print where the guides are installed and open `<name>/SKILL.md` there (the
  name without the `evolution-lab-` prefix) at the anchor after `#`:

```bash
lab guides path
```

- Follow the autonomy in the workspace's `AGENTS.md`. Steps marked `human_only` need the human's
  confirmation at `ask`. Use the MCP tool so Lab can ask. With the CLI, ask in the conversation
  and run the command after a yes. At `yolo`, run the requested steps.
- A step is done only when `lab project status` says `done` — not when a command exits 0.
- Steps do not have to follow the list order: a step is `ready` as soon as what it
  requires holds. Writing the spec first is still the recommended order.

<a id="spec"></a>
## Step `spec` — write the spec

Goal: `spec.answers.json` in the materials root answers the requirements catalogue item by item, and `SPEC.md` next
to it describes what the agent does, what it receives, what a good answer is, and what must not change during
evolution. Lab checks the answers file against its schema; `SPEC.md` retells the answers and does not contradict them.

```bash
lab guides requirements --json
lab scenarios schema connect spec-answers/1
```

1. Choose the delivery path: `campaign` to evolve the agent, `evaluation` to measure it only. The catalogue groups
   are described in `evolution-lab-requirements#groups`.
2. For every item of the path write a status and, when known, an answer: `known`, `unknown` or `needs_decision`.
   `source.kind` is one of the item's `answer_from` (code, run, user, decision); `source.ref` says where it shows.
   A `needs_decision` item is a question for the person; once they choose, rewrite it as `known` with the choice in
   words. A required item is closed only when it is `known`.
3. Assess whether to connect the agent as it is or to port it (section `#connect-or-port` of this skill) and write the result to `fit`.
4. Write `SPEC.md` once the required items of the path are closed.

Fields of your own tool (focus, survey, option lists) go under `client` of the document or of an item; Lab does not
read them. Every other field is checked. A file written for an older catalogue fails at `/catalogue/digest`: rewrite
it for the current catalogue.

Done when: both files exist in the materials root and `spec.answers.json` passes the schema `spec-answers/1`.

<a id="connect-or-port"></a>
### Connect as it is, or port

Decide this while you read the agent, before `SPEC.md`. Look at these items:

| Item | Points to a port (`restructure`) | Points to `hard` |
|---|---|---|
| `agent.entrypoint` | no single place where a turn enters and leaves: the turn is assembled across chains or classifiers | there is one, but wrapping it as a turn is non-trivial |
| `agent.tools`, `world.systems` | tools with side effects are wired inside chains; backends cannot be swapped without rewriting | backends can be swapped through non-trivial adapters |
| `agent.session_state` | global mutable state shared across turns or requests | state exists but can move into the session |
| `agent.config` | — | settings are read from files or a configuration service, not from the declared environment |

The verdict: `restructure` when connecting would in effect rewrite the agent (any sign in the first column); `hard`
when it can be connected through non-trivial adapters; `as_is` otherwise. Write it with the id of the implementation
you read in `fit.implementation`, a short `summary`, and the reasons by item (may be empty for `as_is`,
one to eight otherwise). Tell the person in one of three sentences:
"Connecting as it is — realistic.", "Connecting as it is — hard: <reasons>.",
"I recommend porting onto a template: <reasons>."

On `restructure`, offer the port:

```bash
lab project port <project> --from <implementation> --template <template-source>
```

and the scenario `restructure` (skill `evolution-lab-scenario-restructure`). The person decides; Lab does not block
`connect`.

Assess again when `connect` runs for another implementation of the project (there is one answers file per project)
and when the turn, the tools or the state of the assessed implementation change. Lab does not check that
`fit.implementation` is the implementation you connect. Rewriting the answers file drops the spec confirmation (the
file's digest changed), so confirm again once `spec` is `done`.

<a id="spec-confirm"></a>
## Step `spec-confirm` — confirm the spec

Goal: a person has read `SPEC.md` and confirms it as written.

```bash
lab project confirm <project> --implementation <implementation> --claim spec.confirmed --path SPEC.md --author <name> --link catalogue:requirements --link file:spec.answers.json
lab project confirmations <project>
```

The confirmation holds while the bytes of `SPEC.md`, the answers file and the linked
catalogue stay the same. After an edit, confirm again. A confirmation made before the answers
file existed still holds over `SPEC.md`; confirm again once the answers are written, so that a
later edit of the answers drops it.

Done when: the fact `spec.confirmed` holds for `SPEC.md`.

<a id="package"></a>
## Step `package` — assemble the Lab package

Goal: the implementation has a package entry — the body of a Bundle request without
`schema` — with a launch file and either a campaign manifest or an evaluation input.

- Seams, the neutral turn contract and the launch file:
  `evolution-lab-target-connector#launch-file`.
- Explicit settings and environment: `evolution-lab-target-connector#explicit-settings`.
- Campaign manifest and editable scope: `evolution-lab-campaign-package#editable-scope`.
- Corpus and scorer: `evolution-lab-evaluation-inputs#corpus`,
  `evolution-lab-evaluation-inputs#scorer-protocol`.
- Bundle request and environments: `evolution-lab-preparation`.

```bash
lab project impl set-package <project> <implementation> <entry>
lab project facts <project> --implementation <implementation>
```

The entry path is resolved from the project's materials root; if `package.present` stays false, read the fact's
`source.reason` (`not_configured`, `missing`, `not_a_file`) and `source.entry`.

The fact `package.valid` checks the form only; the environment is checked when the
Bundle is created in the next step.

Done when: the fact `package.valid` holds.

<a id="check"></a>
## Step `check` — check the connection

Goal: the agent starts as a target and answers one turn.

```bash
lab project bundle <project> --implementation <implementation>
lab connect <bundle-digest>
lab project target <project> --implementation <implementation> --bundle <bundle-digest>
lab job status <task-id> --generation <owner-generation>
lab check <connection-id> --target <target-id> --generation <owner-generation> --wait
```

A failed check, and a task in `unknown`, are explained in
`evolution-lab-diagnostics#check` and `evolution-lab-diagnostics#task-states`.

Done when: the fact `check.passed` holds.

**Talk to the agent before the metric is fixed.** `check` only reads health.
To see the agent's real answers on the started target, open a probe session;
no step or fact depends on it:

```bash
lab probe start <project> <implementation> --max-turns 10
lab probe say <session> "your question"
lab probe history <session>
lab probe close <session>
```

`say <session> -` reads the message from stdin; `lab probe list` and
`reset` are the other two commands.

- The ceiling is mandatory: `--max-turns` always, `--max-tokens` and
  `--max-cost` optionally. Lab refuses a turn once any set ceiling is spent
  (`probe_ceiling_reached`); one turn may still overshoot a token or cost
  ceiling, and the turn shows it. A reset keeps the ceiling across the chain.
- An open probe session blocks `check`, `conformance` and operations on that
  target (`probe_sessions_open`): close or reset it, or stop the target.
- A turn may spend a paid model. Cost in money is shown only when a gateway is
  declared for the session (`--budget-ledger`, `--budget-role`,
  `--budget-currency`; see the budget guide); tokens come from the turn's
  `usage` either way. Each turn shows the neutral `agent_outcome`
  (`evolution-lab-target-connector#agent-outcome`).
- After a target stop or a service restart a session is read-only
  (`closed_reason: target_gone`); its history stays readable.
- An implementation without a package starts a target for probes from
  `lab-target-entry.json` (`evolution-lab-target-connector#target-entry`).

<a id="conformance"></a>
## Step `conformance` — run conformance

Goal: the target passes the core session contract.

```bash
lab conformance <connection-id> --target <target-id> --generation <owner-generation> --wait
```

Conformance stops its target; start a fresh one for the baseline. Reading the result:
`evolution-lab-diagnostics#conformance-result`; a common cause of failure:
`evolution-lab-pitfalls#no-ambient-environment`.

Done when: the fact `conformance.passed` holds.

<a id="baseline"></a>
## Step `baseline` — measure a short baseline

**Needs the human's confirmation at `ask`.** The run may call paid models.

Goal: one full fixed evaluation of the connected agent.

The request file holds `kind: "evaluation"`, the Bundle digest, the case selection, seeds
and an explicit evaluation budget (`evolution-lab-evaluation-inputs#evaluation-budget`).

```bash
lab project prepare <project> --implementation <implementation> --request <evaluation-request.json>
lab project target <project> --implementation <implementation> --bundle <bundle-digest>
lab job status <task-id> --generation <owner-generation>
lab project start <project> --implementation <implementation> --prepared <prepared-digest> --target <target-id>
lab status <operation-id>
```

Wait for the target task: repeat `lab job status` until it is `succeeded`, then start.
Only a complete measurement counts: every expected case scored, no stop reason, results
saved (`evolution-lab-diagnostics#persistence`).

Done when: the fact `baseline.measured` holds and the scenario state is `produced`.

## When evidence goes stale

Editing files the Bundle consumed makes `check`, `conformance` and `baseline` stale. If
the edit does not change behaviour (a rename, a comment), a person may carry the
evidence over; the status marks such steps as carried:

```bash
lab project accept <project> --implementation <implementation> --reason <reason>
```

Otherwise repeat the stale steps.
