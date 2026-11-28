# Budget gateway (`evolution_lab.budget`)

> **Who this is for.** Anyone configuring the budget gateway in code. For an explanation of the reservation model and a worked example, read [Budget and cost control](../../../docs/guide/budget.md) first; this page records the configuration and accounting rules. Module: `src/evolution_lab/budget.py`.

The HTTP adapter `budget.create_app` requires `evolution-lab[gateway]`; library callers own its lifecycle. Lab can separately own the target-scoped gateway under a confirmed [execution policy](execution-policy.md). Without a policy, no Lab gateway starts; existing library arguments and lifecycle remain supported. The chat editor also needs `evo-skills[tree]`. These functions do not yet replace a full preflight with a call plan and a confirmed spending limit.

Gateway clients must use `Route.client_token`, not the upstream `Route.api_key`.
Supply a distinct token explicitly or retain the generated token for the lifetime
of the route. Give only the client token to target/editor/scorer; keep upstream
keys inside the trusted gateway owner. This is credential separation, not an OS
sandbox against a malicious caller. Keep existing runs with their original gateway configuration and environment.

A RouterAI/OpenRouter route either pins one `provider` or lists an ordered
`providers` tuple (never both). A set is sent as `only` and `order` in route
order with `allow_fallbacks: false`, so the router may switch only within it;
a single `provider` keeps its previous request body. `routing_observations`
records the requested set as a JSON array in `requested_providers` and the
provider the response reports. `Route.price` still prices the reservation, so
a set should be priced at its most expensive member.

`context_tokens` and `max_tokens` must reflect real request/model bounds; reducing
them to fit concurrent reservations invalidates the cost assumption. Budget must
cover settled costs plus all simultaneous upper reservations. Unknown usage retains
the full reservation. Valid nonnegative integer prompt/completion usage settles from finite
nonnegative numeric `usage.cost` only when protocol currency matches
(OpenRouter USD, RouterAI RUB), otherwise from the tariff. Booleans are
invalid; no usable token usage holds the full reservation even if cost exists.
Reported tokens beyond declared bounds are counted in full and recorded as
`bound_observations`; only settlement above `max(reserved, tariff_actual)`
is an accounting breach (`overrun_requests`) and stops further dispatch.
Neither record can retroactively prevent a provider from
exceeding its advertised bound. Lifting a stop requires `authorize_resume` on the
ledger, an explicit operator authorization recorded beside the archived refusals and
overruns. Retrying a campaign remains explicit and follows its existing
suspension/resume rules; there is no automatic replay.

A failed upstream call recorded by this gateway version leaves one row in
`request_failures (request_id, status, code, observed_at)`; a call cancelled before it was recorded (the task was
cancelled) and calls recorded in the ledger by a gateway before dev46 have none, so `failed_requests` can be lower than
`unknown_requests`:
`status` is the received HTTP status (`NULL` when no response arrived) and `code` a short token, never message text.
Precedence: no HTTP response, `upstream_unavailable`; status >= 400, the body's `error.code`/`error.type` token or
`http_error`; status < 400 with a non-JSON or non-object body, `upstream_invalid`; status < 400 without valid `usage`,
`usage_missing`. HTTP 400–499 (including 429) settles to zero under the unbilled-refusal assumption and records a release audit; 5xx, transport failures, timeouts and invalid successful replies keep the reservation. `summary()` and `/health` report `failed_requests` and
`failed_by_status` (keys are statuses as strings; `transport` is always present and counts requests with no response).
`Route.timeout_seconds` (default 300) bounds the upstream request; hitting it records `upstream_unavailable` with no
status.

`requests.settled_source` is `upstream_cost`, `tariff` or `refused`; older
settled NULL-source rows remain tariff totals. `summary()` and `/health` expose
`settled_upstream_units`, `settled_tariff_units`, `usage_cost_missing` and
`released_refusals`. Missing eligible provider cost (including currency
mismatch) counts in `usage_cost_missing` for recognized protocols.
`release_refused_reservations(authorization=...)` releases only older unsettled
recorded 4xx, audits each release and is idempotent; it does not lift a stop.
`authorize_resume(authorization=...)` remains a separate operator action.
Neither restarts a target/campaign; Lab adds no commands for these actions.

The [policy API](execution-policy.md) adds keyless routes, `client_model`
aliases and policy provider `{only}` or `{order, allow_fallbacks}`. It keeps
`reasoning_effort` in the manifest; operator-run `Route.reasoning_effort`
continues to work. Policy provider pins do not make no-policy manifest pins
globally invalid. Routing reads expose request counts/confirmation history,
not money or per-attempt billing. Readers never migrate ledgers; gateway open
performs the existing ledger migration.
