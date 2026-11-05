# Target server library

Use the target server library, imported from `evolution.tree`, to connect your agent to Evolution. The library implements the `/evolution/v2` HTTP protocol; your backend loads the supplied files, creates sessions and runs agent turns. The packaged `evo-target` uses this same library with a small demonstration backend.

For a step-by-step integration, read [Connecting an agent](connecting-an-agent.md). This page is the API reference: callback signatures, ownership, concurrency, events and a complete offline example.

The library is implemented and shipped in the package. This reference was originally published before implementation under the name F0; the current sections include its later supported contract changes. Editorial updates do not change that contract.

## Import and names

```python
from evolution.tree import (
    TargetServer,        # the /evolution/v2 state machine over a backend
    TargetBackend,       # Protocol: load(tree) -> BackendInstance
    BackendInstance,     # Protocol: open(public, seed) -> BackendSession; close(); optional describe()
    BackendSession,      # Protocol: effective_seed; turn(message) -> BackendReply; close()
    BackendReply,        # frozen: result: TurnResult, truncated: bool = False
    LoadedTree,          # frozen: what load() receives
    BackendError,        # the one exception a backend raises on purpose
    serve_target,        # bind a loopback ThreadingHTTPServer for a TargetServer
)
from evolution.tree import Message, TurnResult, Usage
```

The library exports these eight names and reuses the rollout types `Message`, `TurnResult` and `Usage`. All eleven are also available from `evolution.tree`; the imports above show their defining modules.

The module imports nothing outside `evolution.tree` and the standard library directly: the transitive edge `evolution.tree.artifacts.artifact_copy` → `evolution.lineage.state_io` exists, so the library is not free of the rest of the package, only of any direct dependency on it.

`ReferenceTarget` and `serve_reference_target` keep their current exported names and signatures; the reference target is now a `TargetServer` bound to a `ReferenceBackend` (see [Reference target](reference-target.md#embedding-the-target-in-tests)).

## Constructor

```python
class TargetServer:
    def __init__(
        self,
        backend: TargetBackend,
        *,
        allowed_artifact_root: Path,
        policy: ArtifactPolicyV1,
        seed_mode: SeedMode,
        target_id: str,
        contract_digest: str,
        settings_hash: str,
        runtime_ref_digest: str,
        max_instances: int = 4,
        max_concurrent_sessions: int = 8,
        max_input_bytes: int = 1 << 20,
        max_output_bytes: int = 1 << 20,
        tombstones: int = 1024,
        snapshot_root: Path | None = None,
    ) -> None: ...
    boot_id: str
    def dispatch(self, method: str, path: str, raw_body: bytes = b"") -> tuple[int, bytes]: ...
    def close(self) -> None: ...
```

`backend` is the only positional argument and is the object that decides what a loaded tree means, how a session answers, and what its actual seed is; every other argument is keyword-only.

`allowed_artifact_root` and `policy` bound which candidate roots the server admits and which artifact policy every admitted tree must match, exactly as `ReferenceTarget` takes them today.

`seed_mode` is the campaign's declared seed mode (`exact`, `unpaired` or `none`), which the server validates every session's reported `effective_seed` against — see The backend protocol.

`target_id`, `contract_digest`, `settings_hash` and `runtime_ref_digest` are the four identity strings the server attests in its `/evolution/v2` identity body, and none has a default: a consumer declares its own identity, where the reference target keeps its own constants, and a manifest's optional `target.contract_digest` and `target.settings_hash` pins must equal the values passed here or preflight refuses the target with `pin_mismatch`.

Evolution detects a change on the target's side only through these attested strings. If a switch of LLM provider or model should be visible to a campaign, derive `settings_hash` from the settings that name them (for example, a hash over the provider, the model and the sampling settings) and restart the server after the switch. A campaign resumed against the new value is refused `run_identity_changed` on `identity.target.settings_hash`, and on an unfinished `/8` campaign the operator can then continue in place, re-measure from the next round, or close ([Running campaigns](running-campaigns.md#change-the-provider-mid-campaign)). A value that changes while a campaign is running suspends it `target_changed`, and the same choice follows on resume. A provider switch that the server keeps out of these strings is invisible: the campaign continues as if nothing changed.

`max_instances`, `max_concurrent_sessions`, `max_input_bytes`, `max_output_bytes` and `tombstones` are the server's own capacities, unchanged in meaning from `ReferenceTarget.__init__` today; `max_concurrent_sessions` is a declared capacity the server advertises and does not itself enforce — see Ownership and cleanup.

`snapshot_root` is the directory under which the server keeps its private per-instance snapshots (see Ownership and cleanup); omitted, the server creates and owns a private temporary directory itself.

Validation is the current one: the identifier grammar for `target_id` and the digest strings, positive integers for the capacity arguments, and `allowed_artifact_root` resolved once to an absolute path with `realpath`.

`dispatch` is the pure contract: it never raises, and every failure is a typed refusal drawn from `wire_v2.ERROR_CODES` whose `detail` is a module literal, never caller data.

`serve_target(server, host="127.0.0.1", port=0)` binds a loopback `ThreadingHTTPServer` for a `TargetServer` and is `reference_target_http.serve` renamed; the loopback-only rule (`127.0.0.1`, `localhost`, `::1`) and the daemon-thread serving model are unchanged. A request refused for its `Content-Length` (`413`, or `400` for an invalid value) is answered, then the server half-closes the connection and discards what the client is still sending — at most 64 MiB or 2 seconds — before it closes, so the refusal reaches the client instead of a reset (RFC 9112 §9.6).

## The backend protocol

The server owns the protocol itself — identifiers and their grammar, request-id replay and `request_conflict`, the tombstone deque and `request_expired`, root and policy admission, instance conflicts and capacity, the health and identity bodies, release; the backend decides only the meaning of a loaded tree, how a session answers, and what its actual seed is.

```python
@dataclass(frozen=True)
class LoadedTree:
    instance_id: str
    root: Path                      # the private snapshot, read-only
    policy: ArtifactPolicyV1
    candidate_digest: str
    dependency_digest: str
    artifact: tuple[Entry, ...]     # relative paths, kinds, exec bits — from the snapshot scan
    dependency: tuple[Entry, ...]
    max_output_bytes: int           # the server's advertised bound a reply body must fit
    seed_mode: SeedMode             # the declared mode, so a backend can report a seed honestly


class TargetBackend(Protocol):
    def load(self, tree: LoadedTree) -> BackendInstance: ...

class BackendInstance(Protocol):
    def open(self, public: Mapping[str, Any], seed: int | None) -> BackendSession: ...
    def close(self) -> None: ...
    # optional, detected structurally, never part of the protocol itself:
    # def describe(self) -> Mapping[str, Any] | None: ...
    # def components(self) -> Mapping[str, Any]: ...

class BackendSession(Protocol):
    @property
    def effective_seed(self) -> int | None: ...
    def turn(self, message: Message) -> BackendReply: ...
    def close(self) -> None: ...

@dataclass(frozen=True)
class BackendReply:
    result: TurnResult
    truncated: bool = False
```

`LoadedTree` has these nine fields; `root` is never the candidate root a request named, it is the server's own private snapshot (see Ownership and cleanup), and `max_output_bytes` and `seed_mode` exist so a backend can fit its reply and report its seed honestly without consulting the server again.

`BackendReply` exists because the wire's `truncated` flag is the target's own truncation statement and `TurnResult` has no field for it; the adapter turns `truncated: True` into the `output_truncated` event. `TurnResult.latency_ms` is ignored on input: the server measures the turn from request start and writes its own value, exactly as the reference does today.

The server validates a session's reported `effective_seed` against `seed_mode` and refuses rather than repairs:

| `seed_mode` | request `seed` | `effective_seed` the backend must report |
|---|---|---|
| `exact` | required int | equal to the request's seed |
| `unpaired` | ignored if present; echoed as `requested_seed` | an int the backend chose |
| `none` | must be absent | `None` |

A report outside this table is a backend contract violation: the session is closed on the backend, never registered, and the request answers `500 internal`. A backend without a confirmable seed runs under `none`; the reference backend keeps a per-server counter for `unpaired`, which is the demo's own honest statement that it chose a seed. Nothing on the server's side invents a seed on the backend's behalf.

What `open` receives as `public` is the corpus case's public payload during a campaign, and one fixed probe during `evo tree conformance`: both conformance suites open their probe sessions with `public = {"conformance": true}` and the mode's seed, send one turn per session, and expect a typed reply — a `BackendReply` whose `TurnResult` carries at least one event and a closed outcome — then close and release. The turn's message content is `"conformance"` in every probe but one: the wire suite's input-limit check sends a turn whose content is the letter `c` repeated so that the canonical request body is exactly the declared input limit. When the manifest's `min_input_bytes` is below the smallest turn body, that turn is the smallest body and its content is empty. A backend must treat any session opened with that `public` as a neutral world (an empty history, a reply that does not treat the turn text as business input, whatever the text) rather than refuse it: a refusal from `open` is `500 internal`, a refusal of the sized turn fails the limit check, and the rest of the suite never runs. The probe's shape is part of this contract; a backend that validates `public` strictly must allow it.

During a family-14 campaign (manifest `/15`), Evolution also opens one connection preflight session before its first locally executed rollout, and again after each accepted condition change. It loads the Champion in an instance of its own, opens one session with the first train case's real `public` payload and sends its turns, then checks the events against the manifest's `scorer.metric_contract` and scores the rollout. It is an ordinary session, not a conformance probe and not a measurement: answer it as you answer any case.

The backend reads Candidate material only from `LoadedTree.root`, never from the `candidate_root` a request named and never from any other copy, and what it does with that material is its own; it must not modify the snapshot, since it is read-only and a backend that `chmod`s it is outside the contract; it may hold state per instance and per session and must drop that state in the matching `close()`; every callback it implements must return — a bounded `load` is the backend's own promise, not something the server enforces (see Threads and asynchrony); a reply body must fit `LoadedTree.max_output_bytes`, with the backend's own truncation reported as `truncated` (see Replies and events). Evolution is not an OS sandbox, and the server does not prove that a backend read the snapshot; it proves that the digests it attests are the digests of the bytes it handed the backend.

## The optional `describe()` callback

`describe()` is optional. An instance that implements it — `def describe(self) -> Mapping[str, Any] | None` — states how it assembled the agent from the loaded Candidate: a configuration hash, the source of its settings, its hermeticity mode, where it passed the seed. The server calls it once, right after `load` returns and its shape check passes, and journals the result once per instance in `instance_attested`. Return `None` when you have nothing to report: the instance is created as usual and its body has no `description`, as if the method were absent.

Its object must be flat — identifier keys, scalar values — at most 32 keys and 4096 bytes of canonical JSON. The server validates shape and size and nothing else: **keeping secrets out of it is your obligation**, because the server holds neither the names nor the provenance of your launch secrets. A campaign refuses a description that holds the value of a secret it forwards, as `connection_description_secret` in the connection preflight or, for a later instance, `persistence_failed` / `record_matches_secret` ([when the connection preflight suspends](running-campaigns.md#when-the-connection-preflight-suspends)); report a hash instead of the value. It is never compared, never part of identity, and never shown to the editor or the Reflector.

Detection is structural, exactly like the `open`/`close` shape check: an instance is never required to implement `describe` to be a valid `BackendInstance`, and one that omits it is unchanged. A `describe()` that raises, or that returns anything other than `None` or a mapping the shape/size check accepts, fails the instance's creation the same way a raising `load` does — `500 internal`, no instance registered, the snapshot removed.

## The optional `components()` callback

`components()` is optional. An instance that implements it — `def components(self) -> Mapping[str, Any]` — names the parts of the agent's effective context that the agent builds from the loaded tree, so that a `provenance` report can cite a part smaller than a file. The server calls it once, right after `load` and `describe` return, and serves the result as `components` on the instance-creation reply, on a replay of that reply, and on `GET /instances/{id}`.

The value maps a component identifier (`[A-Za-z0-9._/#:-]{1,256}`) to `{"sha256": <64 lowercase hex characters>, "sources": [<distinct artifact-relative paths>]}`. `sha256` hashes the bytes the agent actually renders for that component; `sources` lists the files it is built from. There are at most 4096 components and 262144 bytes of canonical JSON. The byte limit of the component map (256 KiB of canonical JSON) usually binds before the count limit of 4096 components. The server validates shape and size and nothing else, keeps its own detached copy (the `sources` lists included, so a backend that goes on mutating what it returned cannot change what is served), never compares the value and does not make it part of the instance's identity. A `components()` that raises, returns `None` or returns anything but a mapping the check accepts, fails the creation exactly as a raising `describe()` does: `500 internal`, no instance registered, the snapshot removed.

The instance body counts toward `max_output_bytes` like a turn body: when the creation body with `components` is larger than that limit, the creation fails the same way, with `500 internal`, the loaded instance closed and nothing left behind.

`evo tree conformance` compares the replayed creation identity with `GET /instances/{id}` by value, so a target that reports `components` (or `description`) must serve the same value on creation, on its replay and on `GET`. The library does this automatically; a hand-written target must do it too.

A Target renders one component map per tree under one attested identity: two instances that load the same tree while the four identity strings stay the same must report the same `components` value. The server does not check this, but current campaigns do. A different map inside its reference epoch suspends with `target_components_changed` ([affected-case selection](manifest-v2.md#affected-case-selection)). If you deliberately change how components render, change the attested identity too, for example `settings_hash`, so the operator can accept the change.

```json
{"components": {
  "skills/deposits#description": {"sha256": "…", "sources": ["skills/deposits/SKILL.md"]},
  "skills/deposits#body":        {"sha256": "…", "sources": ["skills/deposits/SKILL.md"]},
  "catalog":                     {"sha256": "…", "sources": ["skills/deposits/SKILL.md", "skills/loans/SKILL.md"]}
}}
```

A skill's description and its body are separate components because they reach the agent differently. The description sits in the catalog that every prompt holds, so a change to it changes the description component and the catalog and can affect every case. The body is read only when the agent opens the skill, so a change to it affects only cases whose `used` list names that body.

## Threads and asynchrony

`serve_target` runs a `ThreadingHTTPServer`: one thread per connection, and every backend callback — `load` included — runs synchronously on a handler thread, never inside a server event loop and never awaited.

One instance's callbacks are serialized under that instance's own lock; callbacks for different instances run concurrently with each other and with the server's own bookkeeping.

The global lock guards only the instance table and the admission bookkeeping it holds — the creation records, the reservations in-flight creations hold on their `request_id`s, and the tombstones — is held only for lookups and mutations, and is never held while a thread waits on an instance lock; no filesystem access and no parsing of caller data ever happens under it; an instance lock is held across that instance's callbacks; unlinking an instance takes its lock first and the global lock second — a thread already holding an instance lock may therefore take the global lock without creating a cycle, which is what lets a `load` run without holding the global lock.

Creating an instance runs admission in three phases, then loading, then publication. Phase A, under the global lock, is the decision the record owns: the `closed` flag, the tombstone, the replay record, the reservation a still-loading creation holds on the same `request_id` — waited on outside the lock like a pending instance, since its record is not written yet — and the pure body checks that read nothing outside the request — that `candidate_root` is a non-empty string, the `strategy`, and the `.`/`..` `instance_id` refusal, which is lexical path arithmetic over a value fixed at construction. Phase B holds no lock at all: the policy payload is parsed and its digest compared, the `candidate_root` containment walk runs its `lstat` per path component, and the snapshot-root containment check runs — every refusal the same code it would otherwise have been, and nothing in the phase mutates server state, so a wedged automount under `allowed_artifact_root` stalls that one admission and neither `health`, another creation nor a release. Phase C, under the global lock again, re-decides what phase B could not hold: the `closed` flag, the tombstone, the replay record and the `request_id` reservation a second time, then the existing-instance, pending and capacity logic, and for a fresh id the insertion of a `loading` table entry whose instance lock the creating thread takes and whose creation `request_id` it reserves before releasing the global lock — so two concurrent creations sharing one `request_id` cannot both publish, whatever instance ids they name. Loading, under the instance lock only, builds the private snapshot and calls `backend.load` (see Ownership and cleanup). Publication computes the identity body, writes the creation record — which replaces the reservation — and marks the entry live under the global lock, briefly, then releases the instance lock; a failed load removes the snapshot and then the entry and its reservation under the same discipline and writes no record.

Waiting on a pending instance's lock happens outside all three phases, and the admission then re-enters at phase A; because phase A decides before the request's own subject is judged, a replay is still the recorded answer rather than whatever the current filesystem would earn, and a reused `request_id` is still `409 request_conflict` rather than the new body's refusal. The `.`/`..` `instance_id` values are refused with `400 invalid_request` although the identifier grammar admits them: a snapshot path built from either names the snapshot root itself or its parent rather than a child of it, and because that refusal is in phase A it precedes the `policy` and `candidate_root` refusals for a request that would earn both.

While an instance is `loading`, a request that addresses it — a replay of the same creation, an identity `GET`, a session open, a release — waits on its instance lock and then observes the published instance or its absence; it is never told `not_ready` for this reason, and the wire's replay rule holds because the creation record exists before any other thread can observe the instance as live. `health` lists live instances only, and other creations and other instances' turns proceed while one instance loads.

A backend that never returns from a callback wedges its own instance: requests addressed to it wait, and `close()` waits for it too, since release must not run a backend's `close` concurrently with its `load`. Nothing else on the server is affected. An asynchronous agent backend runs its own event loop inside its callbacks — one loop per backend on a private thread, or `asyncio.run` per call — since the server offers no `await` of its own; the adapter's per-request deadline disconnects the client, it does not cancel the callback.

## Ownership and cleanup

Attestation digests are computed by the server over a private snapshot it owns, never over the live candidate tree a request named: on admission the server copies the admitted tree's artifact and dependency entries into a fresh, read-only destination, scans that destination under the bound policy, and attests the destination's digests; the backend's `load` receives `LoadedTree(root=<that destination>, ...)`. The snapshot's lifetime runs from that creation to the instance's release or to `TargetServer.close()`, whichever comes first, and it is removed afterward.

`snapshot_root` (a constructor keyword) is where these snapshots live; left unset, the server creates a private directory under the process's temporary directory and removes it in `close()`, and a caller who names one instead must give a directory that already exists, is a directory, and is writable.

A caller-named `snapshot_root` is a container, not the server's own root: the constructor creates one `snapshot-<boot_id>` subdirectory inside it and the server owns and removes only that subdirectory, so two servers may share one named directory, and neither that directory nor anything else the caller left in it is ever removed by `close()`. The constructor resolves the snapshot root with `realpath` and refuses (`ValueError`) a root that equals or contains `allowed_artifact_root`, since a snapshot tree holding the admitted trees would always be wrong; a snapshot root that lies under the allowed root is permitted. The matching half of the rule is enforced at creation time rather than at construction: a creation whose `candidate_root` equals or contains the snapshot root is refused `403 root_not_allowed` — in admission's unlocked phase B, beside the containment walk it belongs with (see Threads and asynchrony) — so no admitted tree's scan can ever include a snapshot and no attested digest can drift as snapshots come and go. The run directory a campaign uses is never written by the target.

The close order is the HTTP server's `shutdown()`, then its `server_close()`, then the `TargetServer`'s own `close()`, in that order, exactly as `evo-target serve` runs it; every request that reaches `dispatch` after `close()` — `health` included — is refused `503 not_ready`, `close()` is idempotent, and its removal of the owned snapshot root is best-effort: a root that cannot be removed is left in place, which is not a reason for the one lifecycle boundary to raise. `close()` sets a `closed` flag under the global lock first, so admission after that point is refused; it then releases every live instance, waiting on each instance's lock and therefore on any load or turn already in flight; a creation that passed admission before the flag but reaches publication after it finds `closed`, closes the backend instance it built, removes its snapshot, and answers `not_ready` — nothing is published after `close()`. Release itself un-publishes the instance before it tears anything down: holding the instance's own lock, it takes the global lock briefly to clear the instance's live flag and, in that same block, to drop every creation record keyed to that instance, so a same-id creation reads the entry as pending rather than reusable and a resend of the instance's own creation `request_id` has nothing left to replay; it then calls `session.close()` on every open session (exceptions swallowed), then `instance.close()`, then removes the snapshot, and last pops the table entry and appends the tombstone. A backend exception during release does not stop the release. A creation replay therefore never outlives the instance it created: while the instance is live a resend of its creation `request_id` returns the byte-identical `200`, and from the moment release un-publishes it that same resend waits on the releasing instance's lock and then answers `410 request_expired` — the same answer a fresh `request_id` for that instance id gets.

A raising callback leaves nothing extra behind: `load` raising (any exception; a deliberate refusal is `BackendError`) answers `500 internal`, registers no instance, records no request and removes the snapshot; `open` raising, or reporting a seed outside the seed table, answers `500 internal`, registers no session, and, if a session object had already been returned, the server calls that session's `close()` before discarding it; `turn` raising answers `500 internal` but leaves the session open, since the wire's own failure statement for one turn is `outcome: target_failed` in a `200` body, which a backend chooses to return through `BackendReply` rather than raise; a reply the wire itself would reject is `500 internal` and is never recorded, the session again left open, so a reply must carry at least one event — including a `target_failed` one, which states its failure in an event rather than in an empty turn.

The server owns its advertised output bound: a turn's `200` body is serialized and measured against `max_output_bytes` before anything is recorded, and a larger body is a backend contract violation (`500 internal`, no record, session left open); the bound travels to the backend as `LoadedTree.max_output_bytes`, and a backend is expected to fit its own reply under it, reporting its own truncation as `truncated: True` rather than relying on the server to cut events.

`max_concurrent_sessions` is a declared capacity the server advertises, not a limit it enforces itself — the closed refusal set has no session-exhaustion code — so it is the client's job to honour it (Evolution's own rollout core acquires one permit per session per instance up to this value); the server counts open sessions per instance only for release, and a backend that must protect its own state against a client that ignores the declared capacity has to bound it itself inside `open`, where raising is `500 internal` like any other backend refusal.

## Replies and events

A session's `turn` returns a `BackendReply` wrapping a `TurnResult(events, usage, latency_ms, outcome, reason=None, provenance=None)`; `outcome` is `ok`, `target_failed` or `candidate_failed`, and `events` is the turn's ordered event tuple.

`candidate_failed` says the target is healthy and the agent's answer is the agent's own **deterministic** defect — a violated output format, an exhausted internal step limit. A wall-clock timeout is never one: neither the rollout's own deadline, which the target never learns, nor a deadline the agent declared for itself, which is a callee-raised timeout and stays `target_failed`. Core cannot enforce this, because the judgement is made inside your process and reaches the wire as one word: it is a documented contract, not a runtime rule. A backend that cannot tell the two apart reports `target_failed`, which costs an indeterminate sample and never a wrong score. `reason` is an optional two-key object, `{"category": str, "code": str}`, both matching the 64-character grammar `[A-Za-z0-9._-]{1,64}`; it is permitted on any outcome, `ok` included, and core carries it into the scorer's trajectory and the editor's brief without concluding anything from it.

`TurnResult` also takes an optional `provenance`, `{"used": [identifier, ...]}`, that reports every part of the loaded tree that was in the agent's context during the turn, including parts that were in the prompt from the start, not only files the agent opened. Each identifier is an artifact-relative path or a component identifier the instance declares in `components()`; there are at most 1024 identifiers of at most 256 characters each, and `[]` means the turn used nothing. It is permitted on any outcome, counts toward `max_output_bytes`, and is left out of the turn body when you do not set it.

Current campaigns, families 14 (`/15`) and 15 (`/16`), store each sample's sorted union of `used` and the digest of its instance's `components` map; the map itself is stored once per measurement. The union is `provenance` and the digest is `components_digest` in `rollout_finished`, under `measurements/<measurement_id>/events.jsonl`; the map is `components_declared` in that journal. A missing turn provenance, more than 1024 identifiers or more than 256 KiB of canonical JSON makes provenance unknown (the field is absent); overflow also sets `provenance_overflow: true`, without truncation. A sample ending before its first turn stores `[]`. Stored provenance, maps and their digests stay outside Editor and Reflector inputs. In family 15 the chat Grouper sees per-component counts from the stored unions; the Reflector and the editor get no field that carries the `used` unions, component maps, their digests or affected-case sets; component names may reach them through the Grouper's group texts. They select [affected cases](manifest-v2.md#affected-case-selection). Removed run families require `2.0.0b9` for historical reading; the wire contract itself is unchanged. See [what a Target should report and how to check it](connecting-an-agent.md#report-what-the-agent-used).

The wire accepts any event with a non-blank string `type`; the scorer input records every event verbatim; the brief a Candidate editor sees shows only these, with only these keys:

| `type` | keys shown | cut |
|---|---|---|
| `assistant_message` | `content` (str) | `text_chars`, `truncated` flag |
| `tool_call` | `name` (str, identifier grammar) | none |
| `tool_result` | `name` (str), `status` (str, identifier grammar) | none |
| `skill_read` | `path` (str, a relative artifact path) | none |
| `output_truncated` | none | none |

Any other event type is accepted on the wire, recorded in the scorer input, and never shown to the editor.

## What the client does with a refusal

The server states `retryable` on every error body it writes (`{"error": {"code", "message", "retryable"}}`), and `true` for exactly three codes: `internal`, `not_ready` and `instances_exhausted`. Since I1 the Evolution client honours it.

Which class a failure falls in is decided by its **code**, never by the flag: `transport_error`, `callee_timeout`, `upstream_unavailable`, `internal`, `not_ready` and `instances_exhausted` are transient; the request-shape and response-shape codes are contract failures; `pin_mismatch`, `policy_mismatch`, `target_epoch_changed`, `limits_incompatible` and `identity_changed` are structural. Only a transient failure is retried, and `retryable: false` can withhold that retry — it can never turn a contract or structural failure into a retryable one. A backend that wants a refusal retried therefore states a transient code and leaves the flag alone; one that wants its transient refusal left alone sets `retryable: false`.

The client's bounds are the operator's, in the manifest, not a target capability: `campaign.call_retries.max_attempts` (1–10, counting the first attempt) for every target call — creating an instance, opening a session, releasing, reading identity — with jittered back-off, and `turn_max_attempts` for the agent's turn alone. Calls within a rollout share its deadline; instance creation and release are outside that rollout. A `500` on a turn is resent **only** under `turn_max_attempts > 1`, which is the operator's declaration that a failed turn leaves their agent's session unchanged; the scaffold's value `1` means a failed turn is never resent, whatever the flag says. A resent call carries the same `request_id`, so a turn the server did record is replayed from that record rather than executed twice. A refusal that was not recorded may still have applied side effects in your backend, so the absence of a replay record alone does not make resending safe.

When the attempts run out, the failure is the *sample's* — an indeterminate try owned by the target — and a campaign family `7` created suspends rather than closes when those tries cross the operator's `campaign.indeterminate` threshold. Nothing here asks anything new of a backend: an unchanged backend simply sees resent `request_id`s for the calls the operator declared retryable.

## A complete offline backend

This backend answers every turn from the first line of the loaded tree's `skills/SKILL.md`, with no model and no network call, reports in `provenance` that it used that file, and is exactly what `tests/tree/target/test_target_server.py::test_f0_example` extracts from this page and runs against the shipped library, so the example is checked against the code:

```python
from collections.abc import Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from evolution.tree import (
    ArtifactPolicyV1,
    BackendReply,
    LoadedTree,
    Message,
    TargetServer,
    TurnResult,
    Usage,
    serve_target,
)


class SkillLineSession:
    def __init__(
        self, skill_line: str, used: list[str], seed: int | None, *, conformance: bool = False
    ) -> None:
        self.skill_line = skill_line
        self.used = used
        self.effective_seed = seed
        self.conformance = conformance

    def turn(self, message: Message) -> BackendReply:
        # A conformance probe can fill the input limit. Return a bounded
        # neutral answer instead of echoing it into an oversized reply.
        answer = "ready" if self.conformance else f"{self.skill_line} | {message.content}"
        result = TurnResult(
            events=({"type": "assistant_message", "content": answer},),
            usage=Usage(prompt=len(message.content), completion=len(answer)),
            latency_ms=0,
            outcome="ok",
            provenance={"used": [] if self.conformance else self.used},
        )
        return BackendReply(result)

    def close(self) -> None:
        pass


class SkillLineInstance:
    def __init__(self, tree: LoadedTree) -> None:
        skill = tree.root / "skills" / "SKILL.md"
        self.skill_line = skill.read_text(encoding="utf-8").splitlines()[0] if skill.is_file() else ""
        self.used = ["skills/SKILL.md"] if skill.is_file() else []

    def open(self, public: Mapping[str, Any], seed: int | None) -> SkillLineSession:
        return SkillLineSession(
            self.skill_line, self.used, seed, conformance=public.get("conformance") is True
        )

    def close(self) -> None:
        pass


class SkillLineBackend:
    def load(self, tree: LoadedTree) -> SkillLineInstance:
        return SkillLineInstance(tree)


def run(allowed_root: Path, port: int = 0) -> tuple[TargetServer, ThreadingHTTPServer]:
    server = TargetServer(
        SkillLineBackend(),
        allowed_artifact_root=allowed_root,
        policy=ArtifactPolicyV1((), (), ("skills/**",)),
        seed_mode="exact",
        target_id="skill-line",
        contract_digest="skill-line-contract-1",
        settings_hash="skill-line-settings-1",
        runtime_ref_digest="skill-line-runtime-1",
    )
    http = serve_target(server, port=port)
    Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return server, http  # the caller shuts `http` down, then calls `server.close()`
```
