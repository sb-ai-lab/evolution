# Pitfalls

[Русский](ru/pitfalls.md)

Most mistakes on this page look harmless while you build a package. They show
up later as a wrong number, a stopped run or an unexpectedly new campaign.
Each entry says what you will notice, why it happens and what to do. The
examples use the order-lookup assistant from [Connect your agent](connect-agent.md).
Read this page once after your first evaluation, and again before your first
paid campaign; each entry links to the page that explains the mechanism.

## Connecting the agent

### The agent works in your shell but not under Lab

**Symptom.** Settings come back empty, the model client cannot find its key,
or a tool cannot find a program it runs. Or worse, the agent runs but behaves
differently than it does on your machine.

**Why.** The Target Service process gets **exactly** the launch file's `env` plus the
secret values you supply at start. Nothing from your shell is inherited, not
even `PATH` or `HOME`. Python runs in isolated mode (`-I`), so `PYTHONPATH` is
ignored as well. Scorers and campaign roles likewise receive only the names in
their `allowed_env`.

**What to do.** Declare in the launch file every variable your agent reads,
whether through `os.environ` or `context.env`, including `PATH` or `HOME` if
its libraries need them (see
[declared settings](connect-agent.md#what-lab-needs-from-your-agent-and-why)).
Declare every value that changes behaviour, including defaults you rely on
implicitly, such as a model name, a region or a feature flag. A value picked up
"from the environment" makes runs irreproducible and is invisible to the run's
identity.

### Capacity numbers are guesses

**Symptom.** Sessions interfere with each other under load, or you change
`max_instances` later and the campaign gets a different identity.

**Why.** `limits.max_instances` and `limits.max_concurrent_sessions` in the
launch file are what Lab and Core believe about your target. Core
advertises `max_concurrent_sessions` but does not enforce it. Both numbers are
part of the campaign's identity.

**What to do.** Declare what your agent really supports, and test it at that
level before a long run. Do not assume one session per instance. Decide the
numbers before the campaign starts.

### The target writes into the measured tree

**Symptom.** Measurements fail verification, or results are recorded against
bytes that differ from the ones that actually ran.

**Why.** The tree your agent loads (`view.root`) is a read-only snapshot, and
its digests are published before the run. Bytecode, caches or logs written
next to your files change the measured bytes. Core checks the Candidate
before and after each scorer call.

**What to do.** Load from `view.root` and write nothing there. Send caches and
logs to `context.work_directory`. Scorer and role programs must also write only
to the output paths they are given.

### A timeout is reported as `candidate_failed`

**Symptom.** Versions of your agent win or lose depending on how slow the
model provider was that day.

**Why.** `candidate_failed` counts against the version being measured, and a
timeout says nothing about that version.

**What to do.** Report every timeout inside the agent, and every failure you
cannot classify, as `target_failed`. Keep `candidate_failed` for defects that
reproduce on the same input, such as a malformed reply or a broken template.
See [Choosing a turn outcome](connect-agent.md#choosing-a-turn-outcome).

### Everything is reported as `target_failed`

**Symptom.** A campaign ends early with `threshold_reached`, or a real defect
never shows up in the scores.

**Why.** `target_failed` samples are not scored. They are retried up to
`sample_retries` times, and the campaign's `indeterminate` limits end the run
when too many pile up.

**What to do.** "When in doubt, `target_failed`" is the right default. But a
defect you *can* identify as deterministic should be `candidate_failed`, so
that the measurement sees it.

### A failed turn raises an exception

**Symptom.** One bad conversation stops the whole campaign.

**Why.** Any exception from your agent is a *fault*, and a fault stops the
campaign at once.

**What to do.** Return an `AgentTurn` for every failed turn. Raise only
`TargetConfigurationError`, and only when the configuration itself is broken
and every sample would fail.

### An async client is created in `build`

**Symptom.** Typically, errors about a missing, closed or different event
loop on the first turn.

**Why.** `build` runs once, before any event loop exists. Each instance then
runs on its own thread and loop.

**What to do.** Create loop-bound resources (async HTTP or model clients) in
the factory, which runs on the instance's loop.

### `describe()` refuses to load

**Symptom.** The target starts, but creating an instance of your agent fails,
so no sample can run.

**Why.** `describe()` must be a flat mapping of scalars with at most 32 keys
and 4096 bytes, and it must not contain any launch secret value.

**What to do.** Return a few flat facts, such as `settings_sha256`, not a raw
model name or URL. Do not repeat values from role or scorer environments,
including strings of 8 characters or more: the judge's model may equal the
agent's model. Keep settings identity in the digest. Never include keys or tokens.

### The final answer appears twice

**Why.** Lab adds the closing `assistant_message` event from `AgentTurn.text`
itself.

**What to do.** Do not add an `assistant_message` event for the final answer.

### The launch file has `null` digests

**Symptom.** Preparation succeeds, but starting the target fails.

**Why.** The file format allows `null` for `contract_digest`, `settings_hash`
and `runtime_ref_digest`, but Lab's launcher passes them to Core's target
server, which requires strings.

**What to do.** Fill all three with 64 lowercase hex characters.

### Conformance is declared, but the agent calls its model

**Symptom.** A conformance run costs money.

**Why.** `{"kind": "core-session-public/1"}` in the launch file is your promise
that the protocol self-test runs offline. Lab cannot verify it.

**What to do.** Detect `public["conformance"] is True` in `open` and answer
those turns without the model, as the example agent does. Otherwise set
`conformance` to `null`.

### A target reply is cut off

**Symptom.** Since Core 0.3.0a32, a check or probe that used to say
`target_mismatch` (or pass) now says `target_unavailable`. Or a campaign
measurement shows `transport_error` under call failures.

**Why.** Core a32 (#140, #169) no longer accepts a reply that ends early as
complete. This covers a reply cut inside its header section, and a chunked
reply cut at its last chunk or trailer. Core records the call as
`uncertain/incomplete_response` in its target-call ledger and raises a
retryable `transport_error`. Before a32, such a reply counted as completed: one
cut in its headers blamed the target with `invalid_response`, and a chunked one
could pass. Lab maps `transport_error` to `target_unavailable` at preflight and
when opening a probe. In a campaign whose manifest declares `call_retries`, the
call is retried. If the retries run out, the sample is `target_failed`, which
counts toward the `target_unstable` threshold.

**What to do.** Look for what cuts the connection: the target process exiting
or crashing mid-reply, or a proxy in between. The `uncertain` ledger row stays even when a retry
succeeds, so the Owner operation ends `cleanup_required` and does not reuse
the target: run `lab cleanup operation <id>` for it.

## Designing the evaluation

### Simulated data is put in `private` and the agent never sees it

**Symptom.** Tools find nothing. For example, every `lookup_order` call
returns "not found".

**Why.** Your agent receives only `public` and the messages. `private` goes to
the environment only.

**What to do.** Route world data through `public` or through fixture files, as
described in [World data and fixtures](evaluation-inputs.md#world-data-and-fixtures).

### The static environment sends an empty message

**Symptom.** The order-lookup assistant answers "Which order number should I
look up?" on every case.

**Why.** A `static` environment sends `public[input_text_key]`, and silently
sends an empty string if that key is missing.

**What to do.** Check that every case has the key (`text` by default in a
campaign manifest).

### Held-out cases leak into feedback

**Symptom.** The campaign reports a clear improvement that does not hold up in
real use.

**Why.** Every case in a campaign corpus is measured, and its results feed the
Editor and Reflector. A case the campaign has seen cannot give unbiased proof.
Lab has no "not for feedback" flag and does not check this.

**What to do.** Keep held-out cases in a separate corpus. Measure the final
Champion on them afterwards with a separate fixed evaluation. Do not copy their
content into the Editor's prompt or into the agent. See
[Held-out cases](evaluation-inputs.md#held-out-cases).

### The scorer leaks the oracle

**Symptom.** The Editor's changes may start quoting the expected answers.

**Why.** The scorer's `public_feedback` reaches the Editor and Reflector.

**What to do.** Put only general diagnostics in `public_feedback`, never
`oracle` content. Use `details` for anything private.

Since b11 (#313), Core treats scorer `allowed_env` values of 8 characters
or more as run secrets. It replaces those secrets with `[REDACTED]` in verdict
notes, `public_feedback` and `diagnostics` before the Reflector and Grouper
receive them. The Editor receives no `public_feedback` entry containing a
secret: the entry is dropped and counted in `feedback_omitted`. An output whose
criterion id contains a secret is unavailable to the roles. These secrets are
not written to the campaign journal.

Never print secrets from the scorer. Scorer traces and the subprocess Editor's
stderr are still stored without masking (#200). A key rotated while a run is
suspended is not masked in evidence scored earlier (#314); revoke a rotated key.

On a `/14` campaign engine (a33, #172) no longer rejects a Reflector note or diagnosis as `oracle_leak` merely
because it names a case the Editor already sees: strings exactly equal to a case's `case_id`, `group_id`, `stratum`,
tag, `case:` or `obs:` id are not needles. Criterion ids that equal an oracle string, and `public` values, stay refused; so
does a longer oracle sentence that merely contains a case id. If your scorer's criterion ids also appear
as oracle strings, the Reflector can still be refused for repeating them (`oracle_leak`); use ids that are not oracle
text. `/11` is unchanged.

### The primary metric is missing from some outputs

**Symptom.** In a campaign, samples are recorded as infrastructure failures
(`scorer_metric_missing`) instead of low scores, and attempts end with too
little evidence to judge.

**What to do.** Make the scorer print the primary metric on every path,
including the path where the agent failed (score it `0.0`). If the scorer
itself cannot decide, exit non-zero.

### A failing donor skill is judged a bad skill

**Symptom.** A skill copied from another agent (a *donor*) works well there
but scores badly in yours, and you conclude it is a poor skill.

**Why.** The usual cause is incompatibility (different tools, prompt layout or
entry point), not the skill's quality.

**What to do.** Record such a result as "incompatible", not as a ranking of
the donor skill.

## Campaigns and identity

### Role calls reserve and spend more on b13 `/16`

**Symptom.** Gateway reservations and spending for role calls grow after moving
new work to Core b13 with a `/16` manifest.

**Why.** Roles receive more text: full episodes and cited observations. Larger
role inputs increase gateway reservations and spending; no measured amount is
available for this pair.

**What to do.** Choose the role evidence volume with `evidence`; see
[Core evidence](../manifest-v2.md#evidence-manifest-16).
Finish `/16` campaigns started on dev60/b12 with their producing pair and state,
not on dev61/b13. Core cannot roll back a `/16` run touched by b13.

### A changed port starts a new campaign

**Symptom.** Two runs you meant as the same campaign get different
identities, so their results cannot be read as one experiment.

**Why.** On Core 2.0.0b13 the campaign identity includes, among other things,
the target's declared limits and identity fields, the `base_url` of the
Editor and Reflector, the Reflector's `probe.base_url`, `sample_retries` and
`max_failed_turns`. On a manifest `/15` (identity `/4`, adaptation profile `campaign_adaptation/7`) it also includes
`reflector.max_consecutive_failures`. The top-level `grouper` is bound too. What Core binds there, by kind: `fields`, its features; `command`, the command and config digests;
a `chat` Grouper, its model, `base_url`, `api_key_env`, `proxy_env`, `retry`, limits
and prompt digest (no credential value); `agent_cli` (Core's `/15`; Lab refuses it), its model, permission, allowed
environment names, limits, and prompt and program digests. If the roles reach their model through a local
gateway, the gateway's port is part of that address. Raising
`max_consecutive_failures` (or changing the Grouper) to get past a role suspension changes the identity:
the resume is refused with `run_identity_changed`.

**What to do.** Pin every address and port before you start. The target port
must also differ from the probe port. Nothing continues a campaign under a
changed identity: the Owner never resumes, and Core refuses to resume a
run whose identity changed (`run_identity_changed`), which is what the legacy
runner's resume relies on.

### Fixed evaluation and campaign disagree on the same agent

**Symptom.** A campaign's measurements and a later fixed evaluation of the
same agent judge some samples differently.

**Why.** Fixed evaluation has its own `max_failed_turns` (new omission default:
resolved `max_turns`) and `call_retries` (default `{max_attempts: 3, turn_max_attempts: 2}`). Neither is read from the campaign manifest,
even when the evaluation is prepared from the campaign's own Bundle.

**What to do.** Pass the campaign's `max_failed_turns` and `call_retries`
explicitly when you prepare the fixed evaluation, if you want the two to judge
the same samples the same way. See
[Evaluation budget](evaluation-inputs.md#the-fixed-evaluation-budget).

## Operations and versions

### target_slot_held when starting a new target

**Symptom.** `lab agent start` refuses `target_slot_held`.

**Why.** A target of an earlier Bundle (often the previous campaign's) is still
`ready`; one target lives per Owner. Before dev46 this was reported as
`binding_mismatch`.

**What to do.** `lab agent list --ready`, then `lab agent stop <id>
--generation <g> --wait`, then start again. `binding_mismatch` now means the
same Bundle with other secrets.

### The process died and nothing picks the run back up on its own

**Symptom.** After your Python process (or the local service) dies, the
operation is not running, and nothing picks it up again by itself.

**Why.** Lab has no reattach: it never rejoins the dead process, and nothing
resumes automatically. A later Owner marks the leftover resources as
orphaned. You read and clean them explicitly.

**What to do.** Keep the process that runs the operation alive until it
finishes. Plan long campaigns accordingly. After a crash, inspect and clean up
explicitly (see [Read the results](results.md)). For a *campaign* operation,
cleanup with confirmed target cleanup makes it `released` (or it may already
be `finished`), and from there `Owner.resume(...)` / `lab resume <id> --target
… --generation …` opens a new start on any ready target of the same Bundle —
the same generation or a later one. Evaluation operations have no such
continuation: start a new one.

### A suspended campaign looks like a failed operation

**Symptom.** `status.control_state` is `"finished"` and `process.exit_code`
is `1` — the usual shape of a failed operation — but the campaign is not
actually done, and `lab status` prints `suspended:` and `continuation:`
lines you have not seen before.

**Why.** An infrastructure failure inside a rollout no longer closes the
campaign as `infrastructure_failed`; Core suspends it instead
(`result.core.suspended: true`, `result.core.suspensions[]` for the reason,
owner and evidence kind) and keeps the run directory. `campaign_outcome`
stays `null`: unknown, not failed.

**What to do.** Read `result.core.suspended` before treating exit code `1` as
a failure. Owner, service and CLI runs continue explicitly on a ready target
of the same Bundle — fix the target (or bring up another one), then
`Owner.resume(...)` / `lab resume <id> --target … --generation …`; `lab status`
prints the exact `continuation:` command. A `cleanup_required`/`orphaned`
operation needs `lab cleanup operation <id>` first. A legacy-runner run
(`<run>-lab-logs/supervisor.lock` exists) continues with `runner.resume_native`
once the target works again, without this admission check. H1 runs have no
continuation in this Lab version; if none of this applies, delete the results
when you no longer need the run directory.
See [Suspension](../../lab/src/evolution_lab/guides/campaign-package/SKILL.md#suspension).

### A family-14 campaign is expected to move with a new wheel

**Symptom.** After changing the Lab or Core wheel, a `/15` campaign started by the previous pair is missing.

**Why.** A state directory belongs to one pair, and a family-14 campaign does not move between pairs by changing the
wheel. Core's issue #206 (a campaign carried over by swapping the wheel) does not arise in Lab: the new pair opens a new
state root and never opens the old one. Likewise Core's #208 concerns `linked_from`, which Lab refuses
(`linked_run_unsupported`), so it does not apply to Lab.

**What to do.** Continue the campaign with the pair that started it, or start a new campaign in a new state directory.

### Deleting a run directory fails on read-only files

**Symptom.** Deleting the results of a campaign (or removing a run's directory by hand) fails with permission errors on
frozen copies of the run.

**Why.** Core freezes the copies it keeps (Core's issue #209).

**What to do.** Make them writable first, then delete: `chmod -R u+w <run directory>`.

### The connection check costs one case and does not check role routes

**Symptom.** A `/15` campaign spends model calls before round 1, or a role's wrong route is not caught at the start.

**Why.** Core's connection check runs the target and the scorer once on one case (a paid call on a real agent). It
checks the answer against the scorer's `metric_contract`; it does not check the routes of the Editor, Reflector or
Grouper (Core's F4 is open).

**What to do.** Count one case in the cost estimate. Check the roles' routes yourself with a tiny campaign. If the check
pauses the campaign, `connection_incompatible` and `connection_preflight_inconclusive` have two exits: a cause outside the
Bundle (the agent's provider, the target's environment) is fixed and the campaign continues; an error in the agent, the
scorer or `metric_contract` needs a fixed package and a new campaign. `connection_preflight_unavailable` has one: fix the
target or the scorer and continue. See
[Suspension](../../lab/src/evolution_lab/guides/campaign-package/SKILL.md#suspension).

### An old state directory is reused with a new version pair

**Symptom.** Unpredictable errors after an upgrade of Lab or Core.

**Why.** A state directory belongs to one Lab/Core version pair. There is no
automatic version check and no migration of Bundles, runs, journals or the
database.

**What to do.** Use a new, private state directory for each version pair.
Never copy an older pair's database or receipts into it.

### The target runs a different Lab version

**Symptom.** A `lab_version_mismatch` warning at preparation, then strange
target failures.

**Why.** The target's Python environment should carry the same
`evolution-lab` version that built the Bundle. Lab warns but does not refuse.

**What to do.** Install the same `evolution-lab` wheel in the target
environment, or check this warning first when a target misbehaves.

### Treating `check` or `conformance` as permission to spend

**Why.** A successful `check` or `conformance` shows that the target is
connected and speaks the protocol. It says nothing about cost. Without an
execution policy, Owner and service start no gateway. An optional confirmed
policy can mount Lab's gateway; it limits routed calls using operator-declared
prices and token bounds. There is no complete paid-call plan preflight (see
[Budget and cost control](budget.md)).

**What to do.** Estimate the cost yourself (cases × repeats × turns, plus
Editor, Reflector and judge calls) and approve it before you start.

## Next

You have reached the end of the guide. Go back to the
[guide index](README.md#pick-your-path) for the other paths, or to
[CLI and local service](cli.md) if you want to drive Lab from the command line.

## Reference

For exact fields and error codes:

- [Pitfalls guide](../../lab/src/evolution_lab/guides/pitfalls/SKILL.md)
- [Target connector guide](../../lab/src/evolution_lab/guides/target-connector/SKILL.md): outcomes, `describe()`, launch file, capacity, conformance
- [Evaluation inputs guide](../../lab/src/evolution_lab/guides/evaluation-inputs/SKILL.md): corpus, environment, scorer protocol
- [Campaign package guide](../../lab/src/evolution_lab/guides/campaign-package/SKILL.md): campaign scale, `sample_retries`, state directories
- [Diagnostics guide](../../lab/src/evolution_lab/guides/diagnostics/SKILL.md): reading `check`, `conformance` and `unknown` states
- [Owner guide](../../lab/src/evolution_lab/guides/owner/README.md): process death, orphaned resources, cleanup

<a id="empty-answers-and-the-reflector-probe"></a>

## Empty answers and the Reflector probe

An `ok` turn with empty or whitespace-only text becomes `target_failed` with
reason `lab/empty_answer` in the default Lab `/2` and `/3` turn layer, even
when events exist. Events and usage are retained; the reason is replaced before
response-budget fitting. Measurements that previously counted empty answers
change meaning. Opt out only explicitly in `/3` with `turn.empty_answer: "ok"`;
command-form `/1` is outside this automatic policy.

A managed Reflector probe needs a manifest declaration and a launch `/3`
object. Null launch means the operator runs it by hand; no manifest probe means
no health check. Exact b15 supports `on_unavailable: continue` as well as
`suspend`; b13 treated every declaration as suspend. Keep older runs on their
producing pair. The
[launch reference](campaign.md#reflector-probe) explains required fields,
`probe_port`, restart limits, cleanup and the independent Owner facts.

### Provider pins belong in a policy for routed roles

Under a confirmed execution policy, routed chat-role manifest fields
`provider_only`, `provider_order` and explicit `allow_fallbacks` are refused
before calls as `manifest_provider_pin`. Move these pins into the policy
before sealing the Bundle. This avoids the old Core #308 request-provider
failure. No-policy manifest pins still pass through to Core; the operator-run
`Route` API is also still supported.

A suspended campaign changes routes through stop target/cleanup → confirm a
reviewed policy with its expected digest → start target of the same Bundle →
resume. The whole gateway block and policy presence stay fixed. Bundle,
Core identity and measurement stay unchanged, and Core does not see the
route change. Lab does not verify model or quantization equality; that is
the human's responsibility. Old Bundles with sealed pins are not migrated.
See the [complete policy workflow](budget.md#execution-policy-and-budget-gateway).

<a id="gateway-accounting"></a>
### Routing evidence is not a billing guarantee

`lab routing OPERATION` preserves policy history and counts calls,
provider mismatches, shared uses and outside uses; it does not audit money or
individual attempts. An absent reported provider is unknown, not a matching
pin. Missing, corrupt, legacy or unattributable ledgers make the read
unavailable with history retained, never zero. The reader cannot create or
migrate a ledger. `report.md` carries no routing audit.

Gateway limits still rely on declared prices and bounds and cover routed calls
only. Accounting stops, held reservations, reviewed 4xx release and explicit
`authorize_resume` keep their existing operator-library rules; there are no
new Lab commands to release reservations or restart a run. See
[Budget accounting](budget.md#reserve-first-settle-later).
