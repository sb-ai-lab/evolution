"""Closed native-v2 journal envelopes; no permissive legacy event conversion."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from math import isfinite
from typing import Any

from evolution.tree.execution.sample_outcomes import SCORED_TERMINALS, disposition_for
from evolution.tree.storage.envelope import (
    Envelope,
    EnvelopeFamily,
    LifecycleTransition,
    _identifier,
    _integer,
    _keys,
    _require,
)

SCHEMA_VERSION = 1
EXTERNAL_OPERATIONS = frozenset(
    {
        "target_materialize",
        "target_release",
        "target_identity",
        "target_open",
        "target_send",
        "target_close",
        "environment_next",
    }
)
SAMPLE_FIELDS = frozenset({"case_id", "sample_id", "candidate_digest"})
FAILURES = frozenset(
    {
        "target_error",
        "environment_error",
        "scorer_error",
        "infrastructure_error",
        "deadline_timeout",
        "cancelled",
        "interrupted",
    }
)
#: A failed call attempt's code: a typed `RolloutInfrastructureError`
#: code or the exception's class name.
ERROR_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def components_digest(components: Mapping[str, Any]) -> str:
    """The SHA-256 of the canonical component map.

    The encoding is `journal_identity.canonical_json` (sorted keys, no
    insignificant whitespace), the same bytes `wire_v2.canonical_body` gives a
    valid map, so a row or attestation can name the map by digest alone.
    """

    from evolution.tree.execution.journal_identity import canonical_json

    return hashlib.sha256(canonical_json(components)).hexdigest()


#: The keys a family-12 row may carry about what ran it.
ROW_PROVENANCE_KEYS = frozenset({"provenance", "provenance_overflow", "components_digest"})


#: The keys a family-14 measurement's
#: `rollout_started` carries about the connection binding it ran under.
CONNECTION_KEYS = frozenset({"connection_binding_digest", "connection_admission"})
#: A lowercase SHA-256 hex digest; shared with `journal_rollout.ConnectionAdmission`.
SHA256 = re.compile("[a-f0-9]{64}")
_SHA256 = SHA256


def _validate_connection(p: Mapping[str, Any]) -> None:
    """The grammar of a start's connection keys.

    Both keys come together. The digest is the `connection_binding/1` SHA-256;
    the admission is `null` or exactly `{operation_id, controller_invocation_id}`
    in the identifier grammar. Whether the journal admits the keys at all is the
    lifecycle's question (the identity's `connection_binding` marker), and
    whether an admission names a compatible preflight is the audit's.
    """

    present = CONNECTION_KEYS & p.keys()
    if not present:
        return
    _require(present == CONNECTION_KEYS)
    digest = p["connection_binding_digest"]
    _require(isinstance(digest, str) and _SHA256.fullmatch(digest) is not None)
    admission = p["connection_admission"]
    if admission is not None:
        _keys(admission, {"operation_id", "controller_invocation_id"})
        _identifier(admission["operation_id"])
        _identifier(admission["controller_invocation_id"])


def _validate_row_provenance(p: Mapping[str, Any]) -> None:
    """The grammar of a row's optional provenance keys.

    `provenance` is the sorted, duplicate-free union of the identifiers the
    turns reported, within the wire's per-turn bound and identifier grammar
    and at most 256 KiB of canonical JSON (ADR-0024, 2026-10-01 amendment);
    a union above either bound is recorded as `provenance_overflow: true` (its
    only spelling) instead, never both. `components_digest` is a SHA-256 hex
    digest. Whether the journal admits the keys at all, and whether the
    digest was declared, is the lifecycle's question.
    """

    if "provenance" in p:
        from evolution.tree.execution.journal_identity import canonical_json
        from evolution.tree.execution.wire_v2 import (
            PROVENANCE_MAX_BYTES,
            PROVENANCE_MAX_USED,
            validate_turn_provenance,
        )

        used = p["provenance"]
        _require(isinstance(used, list) and len(used) <= PROVENANCE_MAX_USED)
        try:
            validate_turn_provenance({"used": used})
        except (ValueError, TypeError):
            _require(False)
        _require(used == sorted(set(used)))
        _require(len(canonical_json(used)) <= PROVENANCE_MAX_BYTES)
        _require("provenance_overflow" not in p)
    if "provenance_overflow" in p:
        _require(p["provenance_overflow"] is True)
    if "components_digest" in p:
        digest = p["components_digest"]
        _require(isinstance(digest, str) and re.fullmatch("[a-f0-9]{64}", digest) is not None)


def _validate_payload(event: str, p: dict[str, Any]) -> None:
    if event == "run_created":
        from evolution.tree.execution.journal_identity import RunIdentityV1

        _keys(p, {"identity", "identity_digest", "producing_version"})
        identity = RunIdentityV1.from_payload(p["identity"])
        _require(identity.digest == p["identity_digest"])
        _identifier(p["producing_version"])
        return
    if event == "journal_dispatch_stopped":
        _keys(p, {"code"})
        _require(p["code"] == "journal_limit_exceeded")
        return
    if event == "components_declared":
        from evolution.tree.execution.wire_v2 import validate_instance_components

        _keys(p, {"components_digest", "components"})
        # `{}` is a declared (empty) map, distinct from declaring none.
        _require(isinstance(p["components"], dict))
        try:
            validate_instance_components(p["components"])
        except (ValueError, TypeError):
            _require(False)
        _require(p["components_digest"] == components_digest(p["components"]))
        return
    if event == "operation_interrupted":
        _keys(p, {"operation"})
        _require(p["operation"] in EXTERNAL_OPERATIONS)
        return
    if event == "sample_imported":
        _keys(
            p,
            SAMPLE_FIELDS
            | {
                "outcome",
                "metrics",
                "critical_pass",
                "effective_seed",
                "dependency_digest",
                "source",
            },
            # A family-12 import keeps the source row's provenance and
            # map digest; `provenance_overflow` is not carried (an overflowed
            # source row imports as unknown).
            {"provenance", "components_digest"},
        )
        _validate_row_provenance(p)
        # A carried row is admitted because it is *scored* (PDR-0013,
        # ADR-0017), so the set is derived from the disposition registry
        # and never spelled out here.
        _require(p["outcome"] in SCORED_TERMINALS)
        _require(isinstance(p["metrics"], dict) and bool(p["metrics"]))
        for name, value in p["metrics"].items():
            _identifier(name)
            _require(type(value) in (int, float) and isfinite(value))
        _require(p["critical_pass"] is None or type(p["critical_pass"]) is bool)
        _require(p["effective_seed"] is None or type(p["effective_seed"]) is int)
        _identifier(p["dependency_digest"])
        source = _keys(
            p["source"],
            {
                "run_id",
                "measurement_id",
                "identity_digest",
                "candidate_digest",
                "retry_ordinal",
                "rollout_digest",
                "scoring_digest",
                "condition_fields",
            },
        )
        for name in ("run_id", "measurement_id"):
            _identifier(source[name])
        for name in ("identity_digest", "candidate_digest", "rollout_digest", "scoring_digest"):
            _require(
                isinstance(source[name], str)
                and re.fullmatch("[a-f0-9]{64}", source[name]) is not None
            )
        _integer(source["retry_ordinal"])
        fields = source["condition_fields"]
        _require(isinstance(fields, list) and all(isinstance(f, str) and f for f in fields))
        _require(fields == sorted(set(fields)))
        return
    if event.startswith("rollout_"):
        allowed = {
            "rollout_started": None,
            "rollout_finished": {
                "completed",
                "turn_budget",
                "time_budget",
                "target_failed",
                "candidate_failed",
                "cancelled",
                "interrupted",
            },
            "rollout_failed": {
                "environment_failed",
                "infrastructure_failed",
                "target_failed",
                "time_budget",
            },
            "rollout_interrupted": {"interrupted"},
        }
        _require(event in allowed)
        required = set(SAMPLE_FIELDS)
        if allowed[event] is not None:
            required.add("outcome")
        # A repair try says so on its start, and only there.
        optional = {"retry_ordinal", "repair"} if event == "rollout_started" else {"retry_ordinal"}
        if event == "rollout_started":
            optional |= CONNECTION_KEYS
        if event == "rollout_finished":
            optional |= ROW_PROVENANCE_KEYS
        _keys(p, required, optional)
        if "repair" in p:
            _require(p["repair"] is True)
        _validate_row_provenance(p)
        _validate_connection(p)
        outcomes = allowed[event]
        if outcomes is not None:
            _require(p["outcome"] in outcomes)
            disposition_for(p["outcome"])
    elif event.startswith("scoring_"):
        required = set(SAMPLE_FIELDS) | {"score_request_id"}
        if event == "scoring_started":
            required |= {"scorer", "input_digest", "input_schema_version"}
        elif event == "scoring_finished":
            required |= {"metrics", "critical_pass"}
        elif event == "scoring_failed":
            required |= {"failure"}
        else:
            _require(event == "scoring_interrupted")
        _keys(p, required, {"retry_ordinal"})
        _identifier(p["score_request_id"])
        if event == "scoring_started":
            _require(type(p["input_schema_version"]) is int and p["input_schema_version"] in (1, 2))
            _require(
                isinstance(p["input_digest"], str)
                and re.fullmatch("[a-f0-9]{64}", p["input_digest"]) is not None
            )
            scorer = _keys(p["scorer"], {"kind", "digest", "recovery"})
            _identifier(scorer["kind"])
            _identifier(scorer["digest"])
            _require(scorer["recovery"] in ("deterministic", "idempotent", "non_replayable"))
        elif event == "scoring_finished":
            _require(isinstance(p["metrics"], dict) and bool(p["metrics"]))
            for name, value in p["metrics"].items():
                _identifier(name)
                _require(type(value) in (int, float) and isfinite(value))
            _require(p["critical_pass"] is None or type(p["critical_pass"]) is bool)
        elif event == "scoring_failed":
            _require(p["failure"] == "scorer_error")
    else:
        operation, _, suffix = event.rpartition("_")
        _require(operation in EXTERNAL_OPERATIONS and suffix in ("started", "finished", "failed"))
        required = (
            {"instance_id", "candidate_digest", "strategy"}
            if operation in ("target_materialize", "target_release")
            else set(SAMPLE_FIELDS) | {"instance_id"}
        )
        if operation in ("target_open", "target_send"):
            required.add("request_id")
        if operation in ("target_send", "environment_next"):
            required.add("turn")
        required.add("retry_ordinal")
        if suffix != "started":
            required.add("latency_ms")
        if suffix == "failed":
            required.add("failure")
        optional = (
            {"target_latency_ms", "usage"}
            if operation == "target_send" and suffix == "finished"
            else set()
        )
        # Call-retry evidence: the 1-based attempt on every record of a
        # retried operation, and the failure's code on its `_failed` record.
        optional.add("call_attempt")
        if suffix == "failed":
            optional.add("error_code")
        _keys(p, required, optional)
        if "call_attempt" in p:
            _integer(p["call_attempt"], 1)
        if "error_code" in p:
            _require(
                isinstance(p["error_code"], str) and ERROR_CODE.match(p["error_code"]) is not None
            )
        for name in ("instance_id", "candidate_digest", "request_id"):
            if name in p:
                _identifier(p[name])
        for name in ("retry_ordinal", "turn", "latency_ms", "target_latency_ms"):
            if name in p and (name != "target_latency_ms" or p[name] is not None):
                _integer(p[name])
        if "strategy" in p:
            from typing import get_args

            from evolution.tree.execution.interaction import MaterializationStrategy

            _require(p["strategy"] in get_args(MaterializationStrategy))
        if "failure" in p:
            _require(p["failure"] in FAILURES)
        if "usage" in p:
            usage = _keys(p["usage"], {"input_tokens", "output_tokens", "unknown"})
            _require(type(usage["unknown"]) is bool)
            for name in ("input_tokens", "output_tokens"):
                if usage[name] is not None:
                    _integer(usage[name])
            _require(
                usage["unknown"] == any(usage[n] is None for n in ("input_tokens", "output_tokens"))
            )
    if "retry_ordinal" in p:
        _integer(p["retry_ordinal"])
    for name in SAMPLE_FIELDS:
        if name in p:
            _identifier(p[name])


def ordinal_limit(sample_retries: int | None, repairs: int) -> int:
    """The last `retry_ordinal` a sample's try may carry (ADR-0016).

    `sample_retries` further sample tries, then one repair try per fault row a
    suspension named -- outside `sample_retries`, so each adds one ordinal. The
    one bound S1, the campaign lifecycle and the audit all read, so the three
    cannot drift.
    """

    return (sample_retries or 0) + repairs


def try_is_retryable(execution: Envelope, scoring: Envelope | None) -> bool:
    """Did this closed try end without a score, so that a further try may follow?

    An indeterminate execution, a scoring terminal without a score, and a
    scoreable execution never scored all leave the sample without a measurement.
    """
    if execution.event == "rollout_started":
        return False
    kind = disposition_for(execution.payload["outcome"]).kind
    if kind == "indeterminate":
        return True
    if scoring is not None and scoring.event in ("scoring_failed", "scoring_interrupted"):
        return True
    return kind == "score" and scoring is None


S1_FAMILY = EnvelopeFamily(
    _validate_payload,
    frozenset(
        {"run_created", "journal_dispatch_stopped", "sample_imported", "components_declared"}
    ),
    # A declaration admits new work like a start does: when the log has
    # no room left beyond its reserved terminals it is refused, not a fault.
    lambda event: event.endswith("_started") or event == "components_declared",
)


class Lifecycle:
    """Contiguous-prefix indexes with atomic, constant-history-cost transitions.

    Preparing a record does not mutate authoritative indexes. The journal commits
    its delta only after fsync; pure reducers prepare and commit immediately.
    """

    def __init__(self) -> None:
        self.sequence = 0
        self.run_id: str | None = None
        self.created: Envelope | None = None
        self.pending: dict[str, Envelope] = {}
        self.operations: set[str] = set()
        self.executions: dict[tuple[str, str], Envelope] = {}
        self.scoring: dict[tuple[str, str], Envelope] = {}
        self.instances: dict[str, Envelope] = {}
        self.instance_ids: set[str] = set()
        self.sample_instances: dict[tuple[str, str], str] = {}
        # `executions`/`scoring` hold each sample's latest try; closed earlier tries
        # move to `tries` as (execution terminal, scoring terminal or None).
        self.tries: dict[tuple[str, str], tuple[tuple[Envelope, Envelope | None], ...]] = {}
        self.ordinal: dict[tuple[str, str], int] = {}
        # How many repair tries each sample started.
        self.repairs: dict[tuple[str, str], int] = {}
        self.imported: dict[tuple[str, str], Envelope] = {}
        self._import_window = True
        #: Family 12: each distinct component map an instance declared,
        #: by its digest; rows and attestations name a map only by that digest.
        self.declared_components: dict[str, Envelope] = {}
        self.cleanup_credits: set[str] = set()
        self.dispatch_stopped = False
        self._identity: dict[str, Any] = {}
        self._schedule: frozenset[tuple[str, str]] = frozenset()
        self._score_requests: set[str] = set()
        self._pending_samples: dict[tuple[str, str], int] = {}
        self._pending_instances: dict[str, int] = {}
        self._materialized: set[str] = set()

    def accept(self, record: Envelope) -> None:
        self.prepare(record).commit()

    def prepare(self, record: Envelope) -> LifecycleTransition:
        v = record.to_payload()
        p, event = v["payload"], v["event"]
        sequence = self.sequence
        _require(v["sequence"] == sequence + 1)
        if sequence == 0:
            _require(event == "run_created")
            identity = p["identity"]
            schedule = frozenset((s["case_id"], s["sample_id"]) for s in identity["schedule"])

            def create() -> None:
                _require(self.sequence == sequence)
                self.created, self.run_id = record, v["run_id"]
                self._identity, self._schedule = identity, schedule
                self.sequence += 1

            return LifecycleTransition(self.reserved_records, create)
        _require(v["run_id"] == self.run_id and event != "run_created")
        identity = self._identity
        if event == "sample_imported":
            pair = (p["case_id"], p["sample_id"])
            entry = next(
                (s for s in identity["schedule"] if (s["case_id"], s["sample_id"]) == pair), None
            )
            _require(self._import_window and entry is not None and pair not in self.imported)
            assert entry is not None
            _require(p["candidate_digest"] == identity["candidate_digest"])
            if identity["target"]["seed_mode"] == "exact":
                _require(p["dependency_digest"] == identity["target"]["dependency_digest"])
                _require(p["effective_seed"] == entry["requested_seed"])
            if {"provenance", "components_digest"} & p.keys():
                # Only a family-12 measurement carries them. The digest names a
                # map declared in the *source* journal, so this journal's
                # declarations are deliberately not consulted (b2 judges
                # `components_unavailable`).
                _require(identity.get("row_provenance") == 1)

            def admit() -> None:
                _require(self.sequence == sequence)
                self.imported[pair] = record
                self.sequence += 1

            return LifecycleTransition(self.reserved_records, admit)
        if event == "components_declared":
            # Only a family-12 measurement (the `row_provenance` marker) has a
            # map to declare; an unmarked journal refuses the record outright.
            _require(identity.get("row_provenance") == 1)
            _require(not self.dispatch_stopped)
            digest = p["components_digest"]
            _require(digest not in self.declared_components)

            def declare() -> None:
                _require(self.sequence == sequence)
                # Defence in depth: the `target_materialize_started` this
                # declaration follows already closed the window (ADR-0017).
                self._import_window = False
                self.declared_components[digest] = record
                self.sequence += 1

            return LifecycleTransition(self.reserved_records, declare)
        if event == "journal_dispatch_stopped":
            _require(not self.dispatch_stopped)

            def stop() -> None:
                _require(self.sequence == sequence)
                self._import_window = False
                self.dispatch_stopped = True
                self.sequence += 1

            return LifecycleTransition(self.reserved_records - 1, stop)
        op = v["operation_id"]
        assert op is not None
        started = event.endswith("_started")
        start_payload = p
        if started:
            _require(op not in self.operations)
            reserved_release = (
                event == "target_release_started" and p["instance_id"] in self.cleanup_credits
            )
            _require(not self.dispatch_stopped or reserved_release)
        else:
            _require(op in self.pending)
            start = self.pending[op].to_payload()
            start_payload = start["payload"]
            family = start["event"].removesuffix("_started")
            if event == "operation_interrupted":
                _require(family in EXTERNAL_OPERATIONS and p["operation"] == family)
            else:
                _require(event.rsplit("_", 1)[0] == family)
                for field_name, value in start_payload.items():
                    if field_name in p:
                        _require(p[field_name] == value)
        key = None
        retry = False
        repair = p.get("repair") is True
        limit = identity.get("sample_retries")
        if SAMPLE_FIELDS <= p.keys():
            key = (p["case_id"], p["sample_id"])
            _require(p["candidate_digest"] == identity["candidate_digest"])
            _require(key in self._schedule)
            _require(key not in self.imported)
            ordinal = p.get("retry_ordinal", 0)
            if event.startswith(("rollout_", "scoring_")):
                _require(("retry_ordinal" in p) == (limit is not None))
            repairs = self.repairs.get(key, 0) + int(repair)
            if "retry_ordinal" in p:
                _require(p["retry_ordinal"] <= ordinal_limit(limit, repairs))
            current = self.ordinal.get(key, 0)
            if event.startswith("rollout_"):
                if started and key in self.executions:
                    previous = self.executions[key]
                    _require(previous.event != "rollout_started")
                    _require(self._pending_samples.get(key, 0) == 0)
                    retryable = try_is_retryable(previous, self.scoring.get(key))
                    if repair:
                        # A repair follows a closed try that no sample retry
                        # may follow: a fault row, or an indeterminate try whose
                        # sample retries are spent (#78). A threshold suspension
                        # grants the latter repair. Which rows a
                        # suspension named is the campaign's and the audit's.
                        spent = limit is not None and (current >= limit or key in self.repairs)
                        _require(
                            limit is not None
                            and (
                                not retryable
                                or spent
                                or identity["profile"] == "rollout_evaluation/1"
                            )
                        )
                        if identity["profile"] == "rollout_evaluation/1":
                            scored = self.scoring.get(key)
                            _require(scored is None or scored.event != "scoring_finished")
                    else:
                        # Sample retries stop at `sample_retries`, and never follow
                        # a repair: an indeterminate repair exhausts its sample.
                        # #194: a journal that carries row provenance also
                        # admits one sample retry after a closed `cancelled` try; the
                        # campaign grants it per row (`restored_rows`), the audit
                        # checks the grant. The ordinal limit and the no-earlier-
                        # repair rule still hold.
                        resumed_cancel = (
                            identity.get("row_provenance") == 1
                            and identity["profile"] != "rollout_evaluation/1"
                            and previous.event == "rollout_finished"
                            and previous.payload["outcome"] == "cancelled"
                        )
                        _require(
                            (retryable or resumed_cancel) and ordinal <= ordinal_limit(limit, 0)
                        )
                        _require(key not in self.repairs)
                    _require(ordinal == current + 1)
                    retry = True
                elif started:
                    _require(ordinal == 0 and not repair)
                else:
                    _require(ordinal == current)
                    _require(
                        key in self.executions and self.executions[key].event == "rollout_started"
                    )
                    _require(self._pending_samples.get(key, 0) == 1)
            elif event.startswith("scoring_"):
                _require(ordinal == current)
                _require(key in self.executions and self.executions[key].event != "rollout_started")
                _require(disposition_for(self.executions[key].payload["outcome"]).kind == "score")
                if started:
                    _require(key not in self.scoring)
                    _require(p["scorer"] == identity["scorer"])
                    _require(p["score_request_id"] not in self._score_requests)
            else:
                _require(key in self.executions and self.executions[key].event == "rollout_started")
                _require(ordinal == current)
        elif "retry_ordinal" in p:
            _require(p["retry_ordinal"] == 0)
        if event == "rollout_started":
            # A marked (family-14 measurement) journal names its binding on
            # every start, the admission nullable; any other journal names none.
            _require(bool(CONNECTION_KEYS & p.keys()) == (identity.get("connection_binding") == 1))
        if event == "rollout_finished" and ROW_PROVENANCE_KEYS & p.keys():
            # Only a family-12 measurement's rows say what ran them, and a
            # digest names a map this journal already declared.
            _require(identity.get("row_provenance") == 1)
            if "components_digest" in p:
                _require(p["components_digest"] in self.declared_components)
        if key is not None and "instance_id" in p:
            _require(self.sample_instances.get(key, p["instance_id"]) == p["instance_id"])
        materialize = event == "target_materialize_started"
        release = event == "target_release_started"
        release_finished = event == "target_release_finished"
        instance = p.get("instance_id")
        if event.startswith(("target_materialize_", "target_release_")):
            _require(p["candidate_digest"] == identity["candidate_digest"])
            _require(p["strategy"] == identity["materialization_strategy"])
            if materialize and instance in self.instance_ids:
                # A failed materialization is retried under the same
                # instance id -- only after its previous attempt closed without
                # finishing, and before any release of it started.
                _require(
                    instance in self.cleanup_credits
                    and instance not in self._materialized
                    and self._pending_instances.get(instance, 0) == 0
                )
            elif release:
                _require(instance in self.instances)
                _require(self._pending_instances.get(instance, 0) == 0)
        consumed_credit = release and instance in self.cleanup_credits
        new_credit = materialize and instance not in self.cleanup_credits
        reserved = self.reserved_records + (1 if started else -1)
        reserved += 2 * int(new_credit) - 2 * int(consumed_credit)
        pending_sample = (
            (start_payload["case_id"], start_payload["sample_id"])
            if SAMPLE_FIELDS <= start_payload.keys()
            else None
        )
        pending_instance = start_payload.get("instance_id")

        def apply() -> None:
            _require(self.sequence == sequence)
            self._import_window = False
            if key is not None:
                if retry:
                    closed = (self.executions[key], self.scoring.pop(key, None))
                    self.tries[key] = (*self.tries.get(key, ()), closed)
                    self.ordinal[key] = p["retry_ordinal"]
                    self.sample_instances.pop(key, None)
                    if repair:
                        self.repairs[key] = self.repairs.get(key, 0) + 1
                if event.startswith("rollout_"):
                    self.executions[key] = record
                elif event.startswith("scoring_"):
                    self.scoring[key] = record
                    if started:
                        self._score_requests.add(p["score_request_id"])
                if instance is not None:
                    self.sample_instances[key] = instance
            if materialize:
                self.instance_ids.add(instance)
                self.instances[instance] = record
                self.cleanup_credits.add(instance)
            elif release:
                self.cleanup_credits.discard(instance)
            elif event == "target_materialize_finished":
                self._materialized.add(instance)
            elif release_finished:
                del self.instances[instance]
            if started:
                self.operations.add(op)
                self.pending[op] = record
            else:
                del self.pending[op]
            if pending_sample is not None:
                self._update_count(self._pending_samples, pending_sample, started)
            if pending_instance is not None:
                self._update_count(self._pending_instances, pending_instance, started)
            self.sequence += 1

        return LifecycleTransition(reserved, apply)

    @staticmethod
    def _update_count(index: dict[Any, int], key: Any, started: bool) -> None:
        count = index.get(key, 0) + (1 if started else -1)
        if count:
            index[key] = count
        else:
            del index[key]

    @property
    def reserved_records(self) -> int:
        return len(self.pending) + 2 * len(self.cleanup_credits) + int(not self.dispatch_stopped)
