---
name: evolution-lab-pitfalls
description: Avoid known traps when building a Lab package - capacity, candidate immutability, hidden environment, held-out cases, donor skills and identity.
---

# Pitfalls

Traps seen when connecting agents to Lab. Each one looks harmless while a
package is being built and shows up later as a wrong measurement or a new
campaign. Examples use a synthetic order-lookup assistant.

<a id="capacity-from-limits"></a>
## Capacity comes from `limits`, not from assumptions

Target capacity is what the launch file declares in
`limits.max_instances` and `limits.max_concurrent_sessions`. Do not assume one
session per instance, and do not raise the numbers beyond what the agent
really supports: the core target server advertises
`max_concurrent_sessions` but does not enforce it. Both values enter the
campaign identity, so changing them later is a new campaign. See
[Capacity](../target-connector/SKILL.md#capacity) and {req:target.capacity}.

<a id="immutable-candidate"></a>
## The candidate is immutable

The target process must not write into the candidate tree: no bytecode, caches
or logs next to the agent's files. Such writes change the measured bytes.
Build the agent from the read-only snapshot the target server provides, and
send caches and logs to the process's own working directory. The same holds
for scorer and editor processes: they write only to output paths supplied for
that purpose, never to their copied input directory.

<a id="no-ambient-environment"></a>
## No ambient environment variables

Everything that changes the agent's behaviour is declared: in the launch
file's `env` and `secret_names` for the target, and in `allowed_env` for the
scorer and roles. Lab does not merge the caller's environment into these
processes. A value the agent picks up "from the environment" anyway (a default
model name, a region, a feature flag) makes runs irreproducible and invisible
to identity. See [Explicit settings](../target-connector/SKILL.md#explicit-settings),
{req:agent.config} and {req:agent.models}.

<a id="test-outside-feedback"></a>
## Held-out (TEST) cases stay outside feedback

Cases used to prove an improvement must not reach the editor or reflector.
Every case in a campaign corpus is measured, and its measurements feed the
editor's brief and the reflector. So:

- keep held-out cases out of the campaign corpus;
- measure the resulting champion on them separately. A fixed evaluation
  prepared from the campaign Bundle measures the seed on the campaign's own
  corpus, so it cannot do this: export the champion's changes, apply them to a
  copy of the agent project yourself, and prepare a separate evaluation-only
  package (`lab-evaluation-input/1`) whose corpus holds the held-out cases
  (see [Selection and coverage](../evaluation-inputs/SKILL.md#corpus));
- do not copy their content into the editor's prompt or the seed.

A `/15` manifest labels corpus rows `split: test` - core keeps them out of the campaign.
Choosing the cases is yours, Lab does not check it. Item:
{req:eval.test_isolation}.

<a id="the-scorer-leaks-the-oracle"></a>
## The scorer leaks the oracle

The scorer's `public_feedback` reaches the Editor and Reflector. Put only
general diagnostics there, never `oracle` content; keep private information
in `details`.

Since b11 (#313), the core treats scorer `allowed_env` values of 8 characters
or more as run secrets. It replaces those secrets with `[REDACTED]` in verdict
notes, `public_feedback` and `diagnostics` before the Reflector and Grouper
receive them. The Editor receives no `public_feedback` entry containing a
secret: the entry is dropped and counted in `feedback_omitted`. An output whose
criterion id contains a secret is unavailable to the roles. These secrets are
not written to the campaign journal.

Never print secrets from the scorer. Scorer traces and the subprocess Editor's
stderr are still stored without masking (#200). A key rotated while a run is
suspended is not masked in evidence scored earlier (#314); revoke a rotated key.

<a id="donor-skills"></a>
## A failing donor skill is not a bad skill

When skills taken from another agent fail inside this one, the cause is often
incompatibility (different tools, different prompt layout, different
entrypoint), not the skill's quality. Record such a result as
incompatibility; do not use it to rank the donor skill.

<a id="identity-addresses"></a>
## Addresses in the identity

On exact core 2.0.0b22, the campaign identity includes the target's declared limits and
identity fields, the `base_url` of the editor and reflector roles, the
reflector's `probe.base_url`, `sample_retries` and `max_failed_turns`. On a manifest `/15` (identity `/4`, adaptation profile `campaign_adaptation/7`) it also binds
`reflector.max_consecutive_failures`. The top-level `grouper` is bound too. What core binds there, by kind: `fields`, its features; `command`, the command and config digests;
a `chat` Grouper, its model, `base_url`, `api_key_env`, `proxy_env`, `retry`, limits
and prompt digest (no credential value); `agent_cli` (core's `/15`; Lab refuses it), its model, permission, allowed
environment names, limits, and prompt and program digests. Raising
`max_consecutive_failures` (or changing the Grouper) to get past a role suspension therefore changes the
identity, and the resume is refused with `run_identity_changed`. If roles
reach their model through a local gateway, pin the gateway's port: a different
port is a different campaign. The target port must also differ from the probe
port. See
[Editor and reflector](../campaign-package/SKILL.md#editor-and-reflector).

For a `chat` Grouper, as for the other chat roles, `api_key_env` has been optional since
b10 (including the current b22 pin). Absent means no authorization headers; declared but empty means refusal.
`base_url` may use HTTP to any host; non-loopback HTTP emits a WARNING (with a
credential declared, it warns that the credential travels unencrypted). Loopback
HTTP has no such WARNING; a role without a declared key still gets the separate
"without credentials" line. Adding or removing the declared key on an existing
campaign requires one `run_identity_changed` (`rebase`) and one operator
`evo tree authorize`. This is Core's identity authorization, not permission to
edit a sealed Lab Bundle or a Lab continuation under a changed identity.

Lab does not continue a campaign under a changed identity. Core's own CLI can:
an operator may run `evo tree authorize` over a Lab run directory. A
non-comparable change then continues as a rebase, and a comparable change
(scorer, corpus, metric, environment) closes the campaign as `stopped` with
reason `condition_changed`. This is an operator action outside Lab's
guarantees; Lab neither suggests nor checks it, and the Lab report shows both
traces (`rebases`, `condition_changed`).

<a id="gateway-accounting"></a>
## Gateway reservations and settled cost differ

The gateway reserves at your tariff using
`min(context_tokens, request JSON bytes)` and the request's maximum output,
after provider/reasoning injection. Keep the real model bounds and prices:
the byte estimate assumes text tokenization and chat formatting fit those
bytes. Already-running calls can settle above reservation and exceed the
limit. More reported tokens create `bound_observations`; only settlement
above `max(reserved, tariff_actual)` stops the gateway as an overrun.

Valid prompt/completion token counts are required even when `usage.cost`
exists. With those counts, finite nonnegative numeric cost is used only for
matching protocol currency (OpenRouter USD, RouterAI RUB); booleans are not
valid tokens or cost. Otherwise settlement uses the declared tariff. No
valid tokens means the reservation stays held. Cache discounts are included
in eligible provider cost, not calculated separately by Lab.
`requests.settled_source` records `upstream_cost`, `tariff` or `refused`;
old settled NULL-source rows remain tariff totals. Read
`settled_upstream_units`, `settled_tariff_units`, `usage_cost_missing` and
`released_refusals` in the summary or `/health`. `usage_cost_missing` counts
recognized-protocol tariff fallbacks, including currency mismatch. The used
total still includes settled amounts plus held reservations.

Upstream HTTP 400–499, including 429, settles to zero and records a release
audit alongside the failure. This assumes those calls were unbilled. 5xx,
transport failures, timeouts and invalid successful replies keep their
reservations. `failed_requests` therefore includes released calls too; it
does not mean all those reservations are held. To release older held 4xx,
use the Python API
`ledger.release_refused_reservations(authorization="Reviewed old 4xx refusals")`.
It touches only unsettled recorded 4xx, audits authorization/count and each
release, returns zero on repetition, and leaves accounting stops in place.
Only `authorize_resume` lifts a stop. Neither action restarts a run; there
is no release CLI.

Model discovery also stays local: `GET /<role>/v1/models` requires the route's
client token, or any member token for a role group. It returns the route
model or all group models in member order as OpenAI model entries owned by
`lab-gateway`, without an upstream call or reservation. A completion still
needs the selected member's token. Unknown roles get
`404 invalid_role_or_model`; wrong or missing tokens get
`401 invalid_gateway_credentials`. See
[Model roles](../campaign-package/SKILL.md#budget-gateway-for-model-roles).

<a id="reply-cut-off"></a>
## A target reply is cut off

Since core 0.3.0a32 (#140, #169), core no longer treats a reply that ends early as complete. This covers a reply cut
inside its header section and a chunked reply cut at its last chunk or trailer. Core records the call as
`uncertain/incomplete_response` in its target-call ledger and raises a retryable `transport_error`. On a31 such a
reply was completed: a header cut blamed the target with `invalid_response`, and a chunked cut could pass. Lab's
preflight and probe open therefore report `target_unavailable` where they used to report `target_mismatch`. In a
campaign with `call_retries`, the call is retried. Exhausted retries make the sample `target_failed`, which counts
toward the `target_unstable` threshold. The `uncertain` ledger row stays even when a retry succeeds, so the Owner
operation ends `cleanup_required` (`lab cleanup operation <id>`). Look for what cuts the connection: the target
exiting mid-reply, or a proxy.

<a id="other-pair"></a>
## A campaign of another pair or another core

A Lab pair (Lab wheel + core wheel) never opens another pair's state. After a re-pin, a campaign that
the previous pair suspended is not an operation of the new pair: `lab resume` does not know it, the
project status shows it with the old `pair` and `detail: unavailable`, and the legacy runner refuses
it with `producing_pair_changed` before core runs. Lab offers two ways on:

- continue it with the pair that started it (a campaign suspended by the dev40 pair continues on
  dev40 / core 0.3.0a27; for `/11` campaigns a28 changed nothing in their behaviour, later cores do — e.g. since a29 an oversized target turn suspends with `target_contract_violated`);
- start a new campaign with the current pair.

Core's own CLI can continue such a run after `evo tree authorize`. This is an operator action
outside Lab's guarantees; Lab neither suggests nor checks it.

<a id="suspended-owner-campaign"></a>
## A suspended Owner campaign is not a failure

An infrastructure failure inside a rollout no longer closes the campaign: core
suspends it instead (`campaign_suspended`), and the run directory is kept.
`OperationStatus.control_state` still reaches `"finished"` and
`process.exit_code` is `1`, which looks like an ordinary failure at first
glance. It is not one: `result.core.suspended` is `true`,
`result.core.suspensions` names the reason, owner and evidence kind, and
`campaign_outcome` stays `null` (unknown, not failed). `lab status` prints
`suspended:` and `continuation:` lines for exactly this state. An Owner,
service or CLI run continues explicitly on a ready target of the same
Bundle — `Owner.resume(...)` / `lab resume <id> --target … --generation …`,
the same generation or a later one, after `lab cleanup operation <id>` if
the operation is `cleanup_required`/`orphaned`. Only a legacy-runner run
(`resume_native`) continues without this admission check; H1 has no
continuation at all.
After a `target_unstable` suspension the status field `resume.target` is
`{requirement: fresh_instance, reason: target_unstable_fresh_required,
excluded: [...]}`: `excluded` lists every target the campaign has used, and the
continuation must run on a target that is not among them. Stop the target if it
is still running (`lab agent stop --wait`), start a new one of the same Bundle
(`lab agent start`, or `lab project target`), then resume on it. `lab resume`
on an excluded target fails with `target_unstable_fresh_required` (HTTP 409,
before any new start is recorded). Stop first: `lab agent start` reuses a
ready target of the same binding, so with the unstable one still running it
would hand you the excluded target back.

A scorer suspension (`scorer_metric_out_of_range`, `scorer_metric_missing`) is repaired
outside the campaign: resume re-measures the named cases with the same scorer, so it helps
only for an accidental value. To use a changed scorer, prepare new inputs and use [campaign operations](../campaign-operations/SKILL.md#propose-continuation-or-a-successor) to check the applicable admission path.
See [Suspension](../campaign-package/SKILL.md#suspension).

<a id="family-14-pair-change"></a>
## Family-14 campaigns, pair change and old copies

A new pair means a new state root: a family-14 (`/15`) campaign does not move to the next pair by swapping the wheel,
and core's #206 does not arise in Lab. Lab refuses `linked_from` in the manifest. Core freezes the
copies of a run it keeps: delete them with `chmod -R u+w <run directory>` first (core's #209). The connection check of
`/15` costs one case and does not check the roles' routes (core's F4 is open).
For `connection_incompatible` and `connection_preflight_inconclusive`, repair a cause outside the Bundle before resuming with it. An error in the agent, scorer or `metric_contract` needs a fixed package and Core admission. See [campaign operations](../campaign-operations/SKILL.md#propose-continuation-or-a-successor) for the available start paths.
`connection_preflight_unavailable` has one: fix the target or the scorer and continue.

<a id="port-and-schemas"></a>
## Ports and scenario schemas

`lab project port` creates a new git repository for the port — in the Lab home by default, or at an absolute
`--into` outside the project's working copy and outside the Lab home — and registers it as an implementation
`ported_from` its source.
Lab never deletes that directory, not even when the implementation is archived; a failed port leaves nothing, and a
crash mid-port leaves a directory the next attempt refuses with `port_destination_exists` (remove it yourself).

A scenario step whose artifact has `check: "schema"` is `done` only when the file parses as JSON and passes the
scenario's schema (`lab scenarios schema <scenario> <name>/<n>` prints it). Otherwise the step is `blocked`:
`artifact_invalid` lists `invalid` records, each naming its file (`artifact`) and the digest of the bytes checked, up to five records per file, `path` being the JSON Pointer; the `digest` of an artifact in `sources[]` can be passed to `lab project confirm --digest`, so a file replaced since the status is refused (`digest_mismatch`); `schema_unusable` means the schema failed, not your
file. A path with `{implementation}` is per implementation: pass the substituted path to `lab project confirm --path`.

## Provider routing and probe availability

Exact b13 treats all declared reflector probes as suspend at admission;
`on_unavailable: continue` has been supported since b14 and remains supported on exact b22. Keep runs with their producing pair; b13 cannot read this declaration.
Under a confirmed execution policy, put provider selection in the policy.
Manifest `provider_only`, `provider_order` and explicit `allow_fallbacks` on
routed chat roles are refused before calls as `manifest_provider_pin`.
This avoids the old Core #308 path where request `provider` objects reached
the gateway and failed as `unpriced_request_features`. Without a policy,
manifest provider settings keep Core's existing pass-through behavior;
operator-run callers keep their existing `Route` provider options.

For a suspended campaign: stop target and wait for cleanup → review and
confirm a new policy with its expected digest → start target of the same
Bundle → resume. The gateway block and policy presence stay fixed. Core
identity and measurement are preserved; Core cannot see a route change and
Lab does not check model or quantization equality. No existing Bundle with
sealed pins is migrated. The [policy guide](../budget/SKILL.md) covers complete
JSON, tokens, keyless local routes and routing audit. An absent provider is
unknown; unavailable ledgers keep history and never become zero counts.
See the [probe launch reference](../campaign-package/SKILL.md#reflector-probe).
