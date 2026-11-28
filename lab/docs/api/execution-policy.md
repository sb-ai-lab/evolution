# Execution policy API

This reference describes dev72 development source on exact Core `2.0.0b17`.
The optional policy lives outside Bundle/prepared descriptors, in the pair's
Owner state. Owner SQLite schema10 refuses v9 and earlier with
`unsupported_schema`, without migration. A fresh state and pair acceptance
are separate prerequisites; this page does not authorize creating either.
For a complete policy JSON and the human workflow, see
[Execution policy and budget gateway](../../../docs/guide/budget.md#execution-policy-and-budget-gateway).

## Policy form and digest

`evolution_lab.execution_policy.parse_policy(document)` validates the closed
`lab-execution-policy/1` and `lab-execution-policy/2` forms and returns an immutable `ExecutionPolicy`.
`policy_digest(document)` computes SHA-256 over canonical UTF-8 JSON (sorted
keys, no whitespace, no NaN, no Unicode normalization). It does not validate
the document; parse before accepting a digest. `to_payload()` returns detached
JSON, retaining optional-field absence for the digest. These calls are offline.

| Object | Required keys | Optional keys |
| --- | --- | --- |
| Policy | `schema`, `gateway`, `routes` | `target_token_name`, `scorer_token_names`, `role_groups` |
| Gateway | `host`, `port`, `ledger_path`, `limit_units`, `ledger_identity`, `currency` | none |
| Route | `client_model`, `endpoint`, `api_key_secret`, `model`, `price`, `max_tokens`, `timeout_seconds` | `provider`, `balance_floor` |
| Price | `input_per_million`, `output_per_million`, `context_tokens` | none |

In `/1`, gateway and routes are required and auto-resume is disabled. In `/2`,
`gateway` and `routes` are optional together; `auto_resume` is optional and
closed with required `reasons`, `max_resumes`, `delay_seconds` and optional
`busy_recheck_seconds` (default 60). Reasons are a nonempty unique list from
`owner.core_raw.suspension_reasons()`. `max_resumes` is an integer of at least 1;
`delay_seconds` is 0 through 31,536,000 and `busy_recheck_seconds` is 1 through
31,536,000, inclusive (365 days). A value outside these bounds is rejected as
`invalid_request` with a safe field pointer. `policy_auto_resume(policy)` returns immutable rules or
`None`. Missing gateway means no gateway is started. Rules come from the target
policy and can be changed only by explicit resume on a target under a new policy.

The gateway is loopback; its ledger path is absolute and currency `RUB` or
`USD`. Endpoints are HTTP(S) without credentials/query/fragment.
`api_key_secret` is a secret name or null for keyless upstreams. Provider form
is exactly `{only: [names]}` or `{order: [names], allow_fallbacks: boolean}`,
only for recognized RouterAI/OpenRouter endpoints. `balance_floor` needs a
positive amount, key and matching protocol currency. `reasoning_effort` is
not a policy field; it remains in the manifest. The guide describes bounds,
role groups and the token-name collision rules.

## Owner methods

| Call | Result |
| --- | --- |
| `await owner.confirm_execution_policy(document, expected_digest=..., author=..., channel=..., note=None)` | `PolicyRecord` |
| `await owner.execution_policy(digest)` | original `PolicyRecord`, or `not_found` |
| `await owner.execution_policies()` | tuple of records ordered by confirmation time/digest |
| `await owner.start_target(connection, secrets=..., client_ref=None, execution_policy=digest)` | existing `TaskRef`; wait/read target as usual |
| `await owner.operation_routing(ResourceRef("operation", operation_id))` | detached `lab-operation-routing/1` mapping |

Confirmation validates/canonicalizes under the state lock and compares the
expected digest before insertion. For invalid `/2` documents, Python Owner raises
`OwnerError` with code `invalid_request` and a safe `path` attribute; service
and CLI put the safe pointer in their error message. Mismatch is `digest_mismatch` with no policy
write. Reconfirming the same canonical document returns the original immutable
metadata. `lab-execution-policy-record/1` has exactly `schema`, `digest`,
`policy`, `author`, `channel`, `note`, `confirmed_at` (numeric epoch seconds).
It stores names, never upstream keys or generated route tokens. It has no
delete/update operation.

An unconfirmed target policy is `policy_unconfirmed`. Owner starts the gateway
using its own installed interpreter and `gateway` extra, in a helper scope
tied to the target. Lab fills route tokens and sends upstream keys only to the
gateway; caller-supplied Lab tokens are refused. `lab-owner-target/2` retains
its existing fields and conditionally adds `execution_policy` as a digest;
no-policy rows omit that field. Target-list schema remains `/1`.

A campaign continuation compares the entire original `gateway` block and
policy presence (including the absence of a gateway within a policy); auto-resume rules may differ. Mismatches are `policy_gateway_changed` and
`policy_continuation_mismatch`. A routed chat role with manifest
`provider_only`, `provider_order` or explicit `allow_fallbacks` is refused as
`manifest_provider_pin` before calls. No-policy manifest pins retain Core's
pass-through behavior. Route changes require stopped operation/target cleanup,
a newly confirmed policy, target start and explicit resume. Bundle and Core
identity stay unchanged; Core cannot see the route change and Lab cannot
attest model/quantization equivalence.

## Service and CLI

All HTTP routes use `/v1`; mutations retain the service's existing request-key
rules. Read routes accept empty bodies and no query or mutation-key headers.

| HTTP | CLI | Result |
| --- | --- | --- |
| none (offline) | `lab exec-policy digest FILE` | bare canonical digest after validation |
| `POST /execution-policies` | `lab exec-policy confirm FILE --expect DIGEST --author AUTHOR --channel CHANNEL [--note TEXT]` | policy record |
| `GET /execution-policies` | `lab exec-policy list` | `{policies: [records]}` |
| `GET /execution-policies/DIGEST` | `lab exec-policy show DIGEST` | policy record |
| `POST /connections/C/target` | `lab agent start C --execution-policy DIGEST [--secrets-file FILE] [--wait]` | task ref |
| `GET /operations/O/routing` | `lab routing O [--json]` | routing read |

The closed confirmation body has exactly `schema:
"lab-service-policy-confirm-request/1"`, `policy` (the complete document),
`expected_digest`, `author`, `channel`, `note` (required, nullable). The CLI
supplies this envelope from the file and options.

Target-start body `/2` has exactly `schema:
"lab-service-target-start-request/2"`, `secrets` and `execution_policy` (digest
or null). Older `/1` with only `schema` and `secrets` is still accepted and
starts without a policy. The CLI uses `/2` when `--execution-policy` is given.

## Closed routing read

`lab-operation-routing/1` has exactly `schema`, `operation_id`, `runs`,
`available`, `reason`, `roles`, `shared`, `outside`. Runs are ordered by start
and contain exactly `start`, `policy_digest`, `author`, `channel`,
`confirmed_at`, `lab_version`. No-policy starts have null digest and null
confirmation fields. Policy metadata comes from each original confirmation,
not from the target's current policy.

When available, `reason` is null; `roles` maps role names to lists of rows
with exactly `endpoint`, `upstream_model`, `client_model`, `requested`,
`reported_provider`, `calls`, `mismatched`. `requested` is a single legacy
provider string, ordered list, or null; `reported_provider` is a response
observation or null. `upstream_model` is the configured value sent upstream,
not proof of the model actually served. `mismatched` counts calls with a known
requested pin/set and a known reported provider outside it. Missing evidence
is unknown, never a confirmed match.

A request whose use set names exactly one start of this operation contributes
a role-row call. Multiple simultaneous uses containing one of this operation's
starts contribute one `shared` call and no role-row call. Valid empty uses
contribute ledger-global `outside`. Other operations' uses do not become this
operation's calls. Overlapping probes remain unsynchronized.

For no policy, return `available: false`, `reason: "no_policy"`, all recorded
runs and null `roles`, `shared`, `outside`. Missing/read-error ledger gives
`ledger_unreadable`, unsupported old columns `ledger_unsupported`, corrupt or
unattributable observations `ledger_invalid`. All retain history and null
aggregates, never partial totals. The read uses only existing ledger bytes;
it never creates/migrates a file. Gateway open alone performs normal migration.

`lab-owner-report/15` conditionally includes this mapping as `routing` for
policy operations, even when routing is unavailable; no-policy reports omit
the key. Campaign view is `/15`, operation status/list `/8`. The panel is
read-only and displays returned facts; `report.md` has no routing audit.
There is no monetary/per-attempt audit in this read. Existing budget-library
accounting and operator authorization remain unchanged.

### Gateway 2xx error bodies

For a 2xx JSON object containing `error` and no `choices`, the gateway records an explicit upstream
refusal and releases the reservation at zero. It records the HTTP status and safe error code, not
the error message, and forwards the response body unchanged. Core b17 `/16` recognizes the refusal;
`/15` classifies it independently.
