---
name: evolution-lab-budget
description: Confirm execution routes, start a target under a policy, change a suspended campaign provider, and inspect gateway accounting.
---

# Execution policy and budget gateway

A human-confirmed execution policy lets Lab run a gateway with a target and
choose upstream routes outside the Bundle. It is optional. Without a policy,
Lab starts no gateway and the existing target, Core role and operator-run
`evolution_lab.budget` paths remain available. Limits cover only calls routed
through the gateway; prices, model bounds and provider billing remain the
operator's responsibility.

## Write the policy

Save the complete example below as `policy.json` **outside the Bundle**.
It declares a keyless local target and a router-backed Editor pinned with
`only`; this example assumes a subprocess Reflector and a non-chat Grouper.
These names, prices and bounds are illustrative, not a current tariff
or instructions to make a paid call. Review them for your integration before
confirmation. Parsing or calculating a digest makes no upstream call.

```json
{
  "schema": "lab-execution-policy/1",
  "gateway": {
    "host": "127.0.0.1",
    "port": 8790,
    "ledger_path": "/private/lab-run/budget.sqlite3",
    "limit_units": 50000000000,
    "ledger_identity": "neutral-campaign",
    "currency": "RUB"
  },
  "routes": {
    "target": {
      "client_model": "target-client",
      "endpoint": "http://127.0.0.1:18880/v1",
      "api_key_secret": null,
      "model": "local-model",
      "price": {
        "input_per_million": 0,
        "output_per_million": 0,
        "context_tokens": 4096
      },
      "max_tokens": 512,
      "timeout_seconds": 30
    },
    "editor": {
      "client_model": "editor-client",
      "endpoint": "https://routerai.ru/api/v1",
      "api_key_secret": "UPSTREAM_ROUTER_KEY",
      "model": "vendor/example-model",
      "price": {
        "input_per_million": 1,
        "output_per_million": 2,
        "context_tokens": 4096
      },
      "max_tokens": 512,
      "timeout_seconds": 60,
      "provider": {
        "only": [
          "provider-a"
        ]
      }
    }
  },
  "target_token_name": "TARGET_GATEWAY_TOKEN"
}
```

The form is closed: required top-level keys are `schema`, `gateway`, `routes`;
optional keys are `target_token_name`, `scorer_token_names`, `role_groups`.
Every `gateway` key and every route key shown above is required, except
`provider`. `gateway.host` must be loopback, `ledger_path` absolute, currency
`RUB` or `USD`; `limit_units` uses 1,000,000,000 units per currency unit.
Each route requires `price` with exactly `input_per_million`,
`output_per_million`, `context_tokens`. Prices are nonnegative; token bounds
and timeout are positive. Endpoints accept HTTP(S), without embedded
credentials, query or fragment. Secret values never belong in the policy.
`api_key_secret: null` sends no upstream authorization header.

`provider` is optional and is exactly `{"only": ["provider-a"]}` or
`{"order": ["provider-a", "provider-b"], "allow_fallbacks": false}` (the
boolean may be `true` when the human chooses router fallback). Names must be
unique. Pins are supported only for recognized RouterAI/OpenRouter endpoints,
not an arbitrary OpenAI-compatible server; local fake protocol URLs use
`/routerai/api/v1` or `/openrouter/api/v1`. A pinned upstream model cannot use
an `@provider` suffix. Price multiple providers at their most expensive rate.
Optional `balance_floor` is positive and needs a key and a recognized protocol
whose currency matches the gateway (RouterAI RUB, OpenRouter USD). The keyless
local route above has neither a pin nor a balance floor.

`reasoning_effort` stays in the campaign manifest: it affects model behavior
and Core identity. It is refused in the policy. The gateway passes the
client's `reasoning` field to the upstream; the library's existing
`Route.reasoning_effort` option remains available to operator-run callers.

## Bind clients and keep secrets separate

The Bundle retains stable client model aliases and gateway URLs. For the
example, the target's launch `env` configures its adapter to call
`http://127.0.0.1:8790/target/v1` with model `target-client`; launch
`secret_names` declares `TARGET_GATEWAY_TOKEN`. The adapter's URL/model setting
names are integration-defined. A chat Editor declares
`base_url: "http://127.0.0.1:8790/editor/v1"`, `model: "editor-client"` and
`api_key_env: "EDITOR_GATEWAY_TOKEN"` in the manifest. That name must be in the
Bundle's declared secrets. Set its output bound no higher than the route's
`max_tokens`. Bind every chat Editor, Reflector and chat Grouper in a policy
campaign to a matching route or group; an unmatched role is refused before
calls with `policy_route_mismatch`.

Lab generates a fresh token per route on every gateway start. It fills
`target_token_name` for the target; Core chat roles use their declared
`api_key_env`, selected by gateway URL and `client_model`. Optional
`scorer_token_names` maps scorer environment names to route names, for example
`{"SCORER_GATEWAY_TOKEN": "editor"}`; those names must be in the scorer's
`allowed_env` and Bundle secret declaration. Optional `role_groups`, for
example `{"roles": ["editor", "reflector"]}`, exposes
`http://127.0.0.1:8790/roles/v1`; member `client_model` values must be distinct.
A group completion uses the token of its selected member.

One environment name may be shared only by consumers of the same route.
Names used for upstream keys must not collide with Lab-filled token names.
Do not supply any Lab-filled token in target/start/resume secrets: Lab refuses
it. Supply upstream key values only at target start using the policy's secret
names; only the gateway receives them. Keep unrelated target/role/scorer
secrets in their existing declared scopes. Lab records names and confirmation
metadata, never key or token values.

For roles routed under a policy, `provider_only`, `provider_order` and even an
explicit `allow_fallbacks: false` in the manifest are refused before calls as
`manifest_provider_pin`. Put those pins in the policy before sealing the
Bundle. Without a policy, manifest provider settings keep their existing Core
pass-through behavior. An old Bundle containing pins cannot be edited for
this continuation; this workflow does not migrate old campaigns.

## Review, confirm and start

Review the actual policy file, including endpoints, prices, limit, key names
and fallback choice. Compute its canonical SHA-256 offline:

```bash
lab exec-policy digest policy.json
```

The command prints a bare digest and needs no service or socket. Save that
reviewed digest as `POLICY_DIGEST`. With the pair's local service running,
confirm exactly the reviewed bytes and start the target explicitly:

```bash
lab exec-policy confirm policy.json --expect "$POLICY_DIGEST" --author operator --channel terminal --note "Reviewed routes and bounds"
lab exec-policy show "$POLICY_DIGEST"
lab agent start CONNECTION --execution-policy "$POLICY_DIGEST" --secrets-file /private/target-secrets.json --wait
```

`CONNECTION` is the connection of the sealed Bundle. The private secrets file
contains the upstream key named `UPSTREAM_ROUTER_KEY` and any ordinary target
secrets, with no generated client tokens. Replace all IDs and paths with your
own. Confirmation stores the policy plus its original author, channel, note
and time in the **pair state**, not the Lab home. `--expect` must match the
file's canonical digest (`digest_mismatch` writes no policy); an unconfirmed
digest cannot start a target (`policy_unconfirmed`). `lab exec-policy list` and
`show` read saved confirmations. Confirmation is a human approval record,
not a check that the upstream serves the claimed model.

Lab uses the Owner's interpreter with the `gateway` extra to start the gateway
in the target's helper scope, waits for health, then starts the target. Target
stop/cleanup also stops the gateway and waits for its cleanup receipt. There
is no automatic gateway restart. A gateway failure stops the target; a running
campaign follows its infrastructure suspension path. The target's reflector
probe inherits the target secrets, including its generated token, without
changing the probe's own lifecycle or `on_unavailable` rules.

Start the prepared campaign/evaluation using the resulting ready target and
its generation in the ordinary `lab start` command. Generated role/scorer
tokens are supplied by Lab; pass only ordinary execution secrets. Interactive
probe sessions use the target's policy as well.

The equivalent Python API, inside an already open Owner and with an existing
connection, is:

```python
from evolution_lab.execution_policy import parse_policy, policy_digest

# document is loaded from the reviewed policy.json; neither call contacts upstream.
parse_policy(document)
assert policy_digest(document) == reviewed_digest
record = await owner.confirm_execution_policy(
    document, expected_digest=reviewed_digest,
    author="operator", channel="python", note="Reviewed routes and bounds",
)
task = await owner.start_target(
    connection, secrets=upstream_and_target_secrets, execution_policy=record.digest,
)
```

Use the existing task wait/status API before using the target; `start_target`
returns a task, not a ready-target promise. `owner.execution_policy(digest)`
and `owner.execution_policies()` read immutable confirmations.

## Change a suspended campaign's provider

1. Check the operation is suspended, its worker has settled and cleanup is
   confirmed. Stop its target explicitly and wait for target/gateway cleanup:
   `lab agent stop OLD_TARGET --generation OLD_GENERATION --wait`.
2. Write policy B outside the Bundle, changing only routes, key names or
   token/group bindings compatible with the existing clients. Keep the whole
   `gateway` block identical, including host, port, ledger path, identity,
   limit and currency. Review B, run `lab exec-policy digest policy-b.json`, then
   `lab exec-policy confirm policy-b.json --expect "$POLICY_B_DIGEST" --author operator --channel terminal`.
3. Start a new target of the **same Bundle**:
   `lab agent start CONNECTION --execution-policy "$POLICY_B_DIGEST" --secrets-file /private/target-b-secrets.json --wait`.
4. Resume explicitly with the new ready target:
   `lab resume OPERATION --target NEW_TARGET --generation NEW_GENERATION --secrets-file /private/execution-secrets.json`.

The Bundle digest, prepared execution, Core identity and measurement stay the
same. Core does **not** see the route change. Lab does not check that two
endpoints/providers serve the same model or quantization; the human must
establish that equivalence before preserving the experiment. This is the
accepted cost of keeping operational routes outside Core identity.

A changed `gateway` block is refused as `policy_gateway_changed`. Adding or
removing policy presence on continuation is `policy_continuation_mismatch`:
a no-policy campaign cannot be moved under a policy or back. No running
operation is rerouted in place. Existing Core resume requirements, Bundle
checks and infrastructure/quality separation still apply.

## Inspect routing and unavailable evidence

`await owner.operation_routing(ResourceRef("operation", operation_id))`,
`GET /v1/operations/OPERATION/routing` and
`lab routing OPERATION --json` expose `lab-operation-routing/1`.
In development source, Owner JSON report `/15` conditionally includes `routing` for policy operations;
the read-only panel shows it too. No-policy reports omit the field. Direct
no-policy reads return `available: false`, `reason: "no_policy"`. `report.md`
has no routing, policies, starts or usage audit.

The read preserves all starts and confirmation metadata (`start`,
`policy_digest`, `author`, `channel`, `confirmed_at`, `lab_version`). Available
role rows contain `endpoint`, `upstream_model`, `client_model`, `requested`,
`reported_provider`, `calls`, `mismatched`. `upstream_model` is the configured
model sent upstream, not an independent observation of the served weights.
`reported_provider` is what the response reported; an absent provider is
unknown, not evidence that the pin matched. Known reported providers outside
the requested set increase `mismatched` and remain visible.

Requests attributed to exactly one operation start contribute to its role
rows. Overlapping uses that include this operation count as `shared`, without
assigning a role call to it; valid empty-use requests count as ledger-global
`outside`. This is a request-count audit, without money or per-attempt detail.
A legacy, missing, corrupt or unattributable ledger makes the read unavailable
(`ledger_unsupported`, `ledger_unreadable` or `ledger_invalid`), with history
retained and `roles`, `shared`, `outside` null. Unknown evidence is never
silently treated as zero. The reader never creates or migrates a ledger;
only opening the gateway performs its normal ledger migrations.

## Operator-run gateway and accounting

Existing `Route`, `Gateway`, `BudgetLedger` and `create_app` callers keep their
behavior and operator-owned lifecycle. A policy does not replace the ledger's
rules: reserve at the declared tariff for bounded input/output; settle from
eligible provider cost with valid token usage and matching protocol currency,
otherwise from tariff; missing usable usage holds the reservation. Reported
tokens beyond a bound are counted in full; settlement above
`max(reserved, tariff_actual)` stops further dispatch as an overrun.

HTTP 400–499 (including 429) releases the reservation to zero under the
unbilled-refusal assumption, with failure and release audit. 5xx, transport
failure, timeout and invalid successful replies hold reservations. The Python
operator flow `ledger.release_refused_reservations(authorization=...)` releases
only older held recorded 4xx, idempotently, without lifting a stop;
`ledger.authorize_resume(authorization=...)` separately authorizes resuming a
stopped ledger. Neither restarts a target or campaign; there are no new Lab
commands for these actions. Read the
[accounting details](../pitfalls/SKILL.md#gateway-accounting) and
[model-role setup](../campaign-package/SKILL.md#budget-gateway-for-model-roles).

A 2xx JSON object containing `error` and no `choices` is accounted as an upstream refusal:
the reservation is released at zero, the status and safe code are recorded, and the response body
is forwarded unchanged. Core `/16` (since b17) handles that refusal; `/15` classifies it independently.
