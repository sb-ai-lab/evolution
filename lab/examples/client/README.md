# H1 native-campaign client: installed offline walkthrough

> **Historical example — Lab dev7/core a8.** These commands describe that
> environment. For the current pair, follow the
> [Owner walkthrough](../../../docs/guide/campaign.md).

This agent-neutral example exercises the public `evolution_lab.client` path
against core's packaged reference target: explicit `check`, preparation with a
complete copied environment, inert `create_run`, asynchronous `run`, `status`,
`stop`, the immutable in-memory result, and `report`. With the exact dev7/a8
producing pair, the generated manifest is native `/4`; its scorer, editor,
Reflector, and fields grouping are synthetic. The packaged roles require
`edit_brief/4` and `reflect_brief/2`. This walkthrough stays on dev7/a8
(PDR-0004). The current pair is Lab dev55 with exact `evo-skills==2.0.0b8`
and the H1 client accepts and writes only native manifest `/15` (the Owner paths also accept `/16`).

The script itself starts the loopback fixture. `connect` and `prepare` never
start a target. Running this explicit command is therefore the launch decision;
use an unused port and a fresh output path:

```bash
cd /work/empty && \
/usr/bin/env -u PYTHONPATH /work/lab-dev7/bin/python -I /work/examples/client/offline.py \
  --output /work/runs/client-h1-a8-001 \
  --python /work/lab-dev7/bin/python \
  --wheel /archives/evolution_lab-0.0.3.dev7-py3-none-any.whl \
  --wheel /archives/evo_skills-0.3.0a8-py3-none-any.whl \
  --target-host 127.0.0.1 \
  --target-port 18765 \
  --locale C.UTF-8 \
  --grace-seconds 45
```

Replace the `/work` and `/archives` placeholders with integration-owned paths.
Run only after creating a fresh environment and installing the exact retained
`evolution-lab==0.0.3.dev7` and `evo-skills[tree]==0.3.0a8` wheels plus their
offline dependencies. The command runs outside the checkout, explicitly removes
`PYTHONPATH`, and uses `-I` so imports come from the installed pair. This guide
does not record an example run or claim release, integration, or consumer
acceptance for the current test-only snapshot.

Every executable and role script recorded in the generated manifest is an
absolute path. The example passes a full `dict(os.environ)` snapshot to
`prepare`, after explicitly setting `LANG=LC_ALL=C.UTF-8` for this selected
Linux stand. Lab preserves that mapping at the core `Popen(..., env=...)`
boundary. It does not normalize Python's startup environment, and core may
filter the environment it passes to individual roles.

The example waits until a public campaign projection is readable, requests a
stop, and then reads the report. If the small synthetic campaign happens to
finish first, the stop receipt is `already_terminal`; a late stop does not
rewrite a naturally exited result. The explicit 45-second grace is a choice for
this fixture. The default formula includes editor, Reflector, scorer, campaign,
and target timeouts plus 10 seconds, but that extra 10 seconds is not a proof
that core's 30-second wrapper cleanup will always finish; callers choose a
positive override when their process contract requires one.

The scaffold's fields grouping needs no grouping subprocess. A real command
grouping must declare its executable, cwd, dependency paths, allowed environment,
and file operands. Its finite `config` is opaque: Lab neither scans nor rewrites
path-looking strings inside it, so the integration must make those references
valid in the saved layout. The empty dispatch guard admits only this local,
credential-free fixture. A
successful `check` does not prove conformance, scorer quality, free execution,
or permission to spend. A real integration must retain its own paid admission
and budget policy. `Connection.editor_kind` describes only the editor: it says
nothing about the Reflector kind and does not establish that execution is free.

The printed `result_in_memory` includes a separate persistence receipt. Even if
publishing `h1-result.json` is `not_attempted` or `failed`, the immutable
in-memory execution result remains available. The report independently combines
Evolution's campaign projection with any valid Lab execution evidence; neither
projection changes the other's meaning.

The output contains `recipe/`, `run/`, `run-lab-logs/`, and `report/`. It is a
temporary synthetic demonstration, not a persistent prepared bundle or a
recoverable owner. See [the H1 API reference](../../docs/api/client-h1.md) for
the complete boundary and migration notes.

Manifest `/4` may also declare a probe. Lab preparation does not call it and S2
cleanup does not own its service or mutable state. The operator must provide a
separate disposable probe service and clean its state independently. This historical client example
does not demonstrate the conformance and fixed-evaluation APIs now available
through the Owner.
