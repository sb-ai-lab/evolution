# Python Owner: neutral offline flows

Two current examples, both offline (no model, network or credential), for the
version pair of this checkout, `evolution-lab==0.0.3.dev81` / exact
`evo-skills==2.0.0b22` (`just versions` prints both). `evaluation.py` needs Lab
`0.0.3.dev72` or later, because it builds its project with
`evolution_lab.quickstart`.

- [`evaluation.py`](evaluation.py) — fixed evaluation of the project skeleton
  with explicit Owner calls: Bundle → conformance → evaluation → report.
  Walked through in
  [What `lab evaluate` does inside](../../../docs/guide/first-evaluation.md#what-lab-evaluate-does-inside).
- [`campaign.py`](campaign.py) — campaign: the core scaffold's sample editor,
  reflector and scorer improve a synthetic agent; check → campaign → report →
  diff → export. Expected output and the meaning of each outcome are in
  [Run a campaign](../../../docs/guide/campaign.md#what-a-finished-campaign-looks-like).
- [`demo_readout.py`](demo_readout.py) — reads a finished campaign example's
  state and writes `conversations.md`, `report.md`, `changes.diff` and
  `export.zip`; `just demo-campaign` runs it ([The demo](../../../docs/guide/demo.md)).
  `just demo` does not run any of these scripts: it writes a project skeleton
  with `lab new-project` and measures it with `lab evaluate`.

To measure your own agent, you do not need these scripts: start with
`lab new-project` and `lab evaluate`
([Measure your own agent](../../../docs/guide/first-evaluation.md#measure-your-own-agent)).

[A tour of the examples](../../../docs/guide/examples.md) explains the files both
examples write and the order to read them in.

Prepare the `work/` layout of [Install](../../../docs/guide/install.md): the Lab
wheel built from this checkout and the exact core wheel in `work/wheels/`, both
installed in `work/lab-venv/`. Source/wheel provenance of released pairs is in
[RELEASES](../../docs/RELEASES.md). Use a fresh state directory for each
version pair, and keep old runs with the wheels that produced them. Both current
examples use the pair's installed interpreter and take the same arguments. Run them from `work/`; a `--python`
path with `..` in it fails with `environment_invalid`. The commands below name
this pair's wheel files; for another pair, use its versions:

```sh
lab-venv/bin/python evolution-lab/examples/owner/campaign.py \
  --python lab-venv/bin/python \
  --lab-wheel wheels/evolution_lab-0.0.3.dev81-py3-none-any.whl \
  --core-wheel wheels/evo_skills-2.0.0b22-py3-none-any.whl \
  --state-directory lab-state-campaign-new \
  --source-directory campaign-source-new \
  --port 18766
```

The rest of this page describes `evaluation.py` and the historical
`offline.py` in detail.

[`evaluation.py`](evaluation.py) is the complete fixed-evaluation demonstration for this pair. It
writes the same project skeleton as `lab new-project` (`evolution_lab.quickstart.new_project`): a
stub agent `agent/my_agent.py` with no model, the adapter `agent/adapter.py`, a 3-case corpus and
an offline subprocess scorer. `resolve_project` fills in the `"lab:auto"` fields in a run copy
(`<source>/.lab/run/`). The script then starts the agent through the Lab target launcher
(`lab-target-launch/3`, `evolution_lab.target.launch`) and makes its own explicit Owner calls to:

1. create an evaluation-only Bundle from the run copy in a fresh state directory;
2. prepare a selection of all three cases (one sample each, the skeleton's defaults);
3. explicitly start a target and run public conformance;
4. require confirmed conformance-helper and target cleanup and confirm the old
   target is `stopped`;
5. explicitly start a different target, run fixed evaluation, require 3 of 3
   samples scored, call the public report reader, and explicitly stop that
   target.

Run it with the Python environment where the pair is installed, and pass the wheel files you
installed the pair from (the core wheel is the one you verified against its `SHA256SUMS`, see
[Install](../../../docs/guide/install.md)).
Both directories must be absent, and the chosen loopback port must be free:

```sh
lab-venv/bin/python evolution-lab/examples/owner/evaluation.py \
  --python lab-venv/bin/python \
  --lab-wheel wheels/evolution_lab-0.0.3.dev81-py3-none-any.whl \
  --core-wheel wheels/evo_skills-2.0.0b22-py3-none-any.whl \
  --state-directory lab-state-evaluation-new \
  --source-directory evaluation-source-new \
  --port 18765
```

The example has no provider, external framework, credential, copied external
corpus, editor, or reflector. Its only agent is the stub of the project
skeleton; Lab supplies and drives the launcher process, so the example writes no
launcher code of its own. `max_output_bytes=8192` leaves enough response
budget for public conformance; `max_instances=1` is a valid declared capacity.
It prints only neutral lifecycle facts. Full conformance and measurement reports,
corpus rows, trajectories, launcher/scorer output, and state artifacts stay in
the private state tree.

Conformance is an explicit offline integration declaration, not proof that
arbitrary integration code is free or ready for paid use. Once a request may
have reached the target, the target must be fully cleaned up whatever happens
next: success, failed checks, Stop, an exception or a lost response. When Lab
cannot confirm what happened to a process, a mutation, a cleanup, a result read
or a saved result, it keeps that uncertainty visible and may keep a blocker.
There is no reattach, migration, automatic retry, automatic target restart, or GC, and
for this fixed evaluation, no resume (a campaign operation, like
`campaign.py`'s, can instead be continued explicitly with `Owner.resume`
after a suspension — see [Run a campaign](../../../docs/guide/campaign.md#suspension)).

## Historical campaign example: dev7/core a8

The older [`offline.py`](offline.py) walkthrough remains available for its
dev7/a8 producing pair and native campaign `/4`. It is documented below and is
not an instruction to rewrite or migrate its Bundle or state into a newer version pair.

[`offline.py`](offline.py) consumes an existing immutable Bundle, creates a new
PreparedExecution through the separate S1 store, and shows the explicit S2
lifecycle:

1. `prepare_campaign`, then `Owner.open` and `bind`;
2. `start_target` and `wait` for its service task;
3. optional `check`;
4. `start`, manual `status` polling, optional cooperative `stop`, and `report`;
5. optional evidence-based cleanup for a terminal blocker;
6. explicit target stop and Owner close.

For that older campaign path, create the Bundle first with the neutral
[`examples/preparation_s1`](../preparation_s1/README.md) flow. Its execution
environment must contain installed `evolution-lab==0.0.3.dev7` and exact
`evo-skills==0.3.0a8`, backed by the exact retained wheel archives recorded by
the Bundle. Do not use `PYTHONPATH` to replace installed Lab code, update an old
Bundle in place, or reuse an older Bundle with a different producing
environment. That historical dev7 implementation wheel was a test-only
development snapshot, not a published or integrated delivery.

The Bundle supplies the external launcher and all integration-owned inputs.
This example supplies no agent, corpus, scorer, prompt, credential, or connected
service. It does not install dependencies, create the Bundle, run conformance,
or grant paid admission.

Choose one fresh private state root for both `PreparationStore` and `Owner` in
this dev7/a8 pair. It must never have been opened by dev6/a5. Do not copy an old
SQLite DB or cleanup receipts into it; old Bundles and reservations remain with
their producing pair.

Run with the exact producing Python and the exact target and execution secret
*names*:

```sh
/work/owner-venv/bin/python examples/owner/offline.py run \
  --state-directory /work/lab-state-dev7-a8-new \
  --bundle-digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --target-secret TARGET_TOKEN \
  --execution-secret EDITOR_TOKEN \
  --check
```

The program prompts for each value without echo and passes explicit maps to the
Owner. Names must be the exact target set `T` and execution set `E`; omit the
options when the corresponding set is empty. A shared name is prompted once and
sent with the same value. Owner does not merge the ambient environment.
For command grouping, `E` also includes its declared `allowed_env` names that
intersect Bundle secret names. Grouping `config` remains opaque and any
path-looking locators inside it are the integration's responsibility.

`--check` is explicit H1 preflight, not conformance or paid readiness.
`--request-stop` asks for cooperative Stop immediately after operation
acceptance; status polling continues to terminal evidence. `--cleanup-on-blocker`
is an explicit cleanup request only if the resulting operation is
`cleanup_required` or `orphaned`. It is not automatic failure recovery.

The example prints only public projections and report availability. It does not
print `ReportResult.report`, because full reports and arbitrary integration logs
are private and have no universal redaction guarantee. Bundle-resident inputs
are read-only during execution; scorer and editor outputs must use explicit
integration-owned output paths, never their copied input cwd.

A `/4` probe is a separate operator-owned disposable service. Owner does not
start it and target/campaign cleanup does not clear its mutable state. That
dev7 walkthrough does not run S3 conformance or fixed evaluation; the historical
`core-a5-session-public/1` token does not provide current conformance.

Manual release is deliberately a separate command. Use it only after a human
has verified the exact external remnants and fixed target port are clear:

```sh
/work/owner-venv/bin/python examples/owner/offline.py manual-release \
  --state-directory /work/lab-state-dev7-a8-new \
  --kind operation \
  --id 0123456789abcdef0123456789abcdef \
  --reason 'External remnants and fixed port verified clear' \
  --attest-external-cleanup-complete
```

Do not put secrets in the reason. Manual release removes only linked durable
reservations while preserving unknown cleanup and the campaign outcome. It is
not a kill-by-PID action or positive cleanup evidence.

The complete lifecycle, conservative reuse conditions, recovery boundary, and
privacy limitations are in the
[`Python Owner guide`](../../src/evolution_lab/guides/owner/README.md).
