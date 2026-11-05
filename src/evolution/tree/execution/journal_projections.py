"""Pure versioned public views; retained prefix identity is not campaign authority."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evolution.tree.execution.journal import Snapshot
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.journal_recovery import recovery_plan
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.execution.sample_outcomes import SampleDisposition
from evolution.tree.storage.envelope import Envelope, SchemaError, is_identifier

_COMMON = {
    "schema_version",
    "kind",
    "run_id",
    "run_identity_digest",
    "candidate_digest",
    "last_sequence",
    "prefix_digest",
    "valid_bytes",
    "consistency",
    "complete",
    "journal_integrity",
    "diagnostics",
    "lower_bound",
    "references",
}
_SAMPLE = {
    "case_id",
    "sample_id",
    "candidate_digest",
    "execution_state",
    "execution",
    "scoring_state",
    "evaluation",
    "metrics",
    "critical_pass",
}


def _require(condition: bool) -> None:
    if not condition:
        raise SchemaError("invalid public projection")


def _keys(value: Any, names: set[str]) -> None:
    _require(type(value) is dict and set(value) == names)


def _identifier(value: Any) -> None:
    _require(type(value) is str and is_identifier(value))


def _sha(value: Any) -> None:
    _require(type(value) is str and re.fullmatch("[a-f0-9]{64}", value) is not None)


def _integer(value: Any) -> None:
    _require(type(value) is int and value >= 0)


def _disposition(value: Any) -> None:
    if value is None:
        return
    _keys(value, {"outcome", "kind", "owner", "reason"})
    try:
        SampleDisposition(**value)
    except (TypeError, ValueError):
        raise SchemaError("invalid public disposition") from None


def _validate(data: Any) -> None:
    _require(type(data) is dict)
    kind = data.get("kind")
    _require(kind in ("status", "events", "result"))
    extra = (
        {"events"}
        if kind == "events"
        else {"samples", "recovery_actions", "owned_instances"}
        if kind == "status"
        else {
            "samples",
            "claim_scope",
            "campaign_outcome",
            "selection",
            "initial_champion",
            "final_champion",
        }
    )
    _keys(data, _COMMON | extra)
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    for name in ("run_id", "candidate_digest"):
        _identifier(data[name])
    for name in ("run_identity_digest", "prefix_digest"):
        _sha(data[name])
    for name in ("last_sequence", "valid_bytes"):
        _integer(data[name])
    _require(data["last_sequence"] > 0)
    _require(data["consistency"] in ("observed", "durable"))
    _require(type(data["complete"]) is bool and type(data["lower_bound"]) is bool)
    _require(data["journal_integrity"] in ("complete", "incomplete_tail", "corrupt_suffix"))
    _require(data["complete"] == (data["journal_integrity"] == "complete"))
    _require(data["lower_bound"] == (not data["complete"]))
    _require(type(data["diagnostics"]) is list)
    for diagnostic in data["diagnostics"]:
        _keys(diagnostic, {"scope", "code", "offset", "category", "length", "digest"})
        _require(diagnostic["scope"] == "journal" and diagnostic["code"] == "journal_unavailable")
        _require(
            diagnostic["category"] in ("incomplete_tail", "corrupt_suffix", "incomplete_creation")
        )
        _integer(diagnostic["offset"])
        _integer(diagnostic["length"])
        _sha(diagnostic["digest"])
    _require(type(data["references"]) is list and len(data["references"]) == 1)
    reference = data["references"][0]
    _keys(reference, {"version", "kind", "run_id", "locator", "content_digest"})
    _require(type(reference["version"]) is int and reference["version"] == 1)
    _require(reference["kind"] == "journal_record" and reference["run_id"] == data["run_id"])
    _require(reference["locator"] == f"events.jsonl#bytes=0:{data['valid_bytes']}")
    _require(reference["content_digest"] == data["prefix_digest"])
    if kind == "events":
        _require(type(data["events"]) is list and len(data["events"]) == data["last_sequence"])
        records = tuple(
            Envelope.from_payload(record, family=S1_FAMILY) for record in data["events"]
        )
        recovery_plan(records)
        _require(records[0].to_payload()["run_id"] == data["run_id"])
        _require(records[0].payload["identity_digest"] == data["run_identity_digest"])
        _require(records[0].payload["identity"]["candidate_digest"] == data["candidate_digest"])
    else:
        _require(type(data["samples"]) is list and bool(data["samples"]))
        seen = set()
        for sample in data["samples"]:
            # `tries` appears only for an identity declaring `sample_retries`.
            _keys(sample, _SAMPLE | {"tries"} if "tries" in sample else _SAMPLE)
            if "tries" in sample:
                _integer(sample["tries"])
                _require(
                    (sample["tries"] == 0)
                    == (sample["execution_state"] in ("not_started", "imported"))
                )
            for name in ("case_id", "sample_id", "candidate_digest"):
                _identifier(sample[name])
            key = (sample["case_id"], sample["sample_id"])
            _require(key not in seen and sample["candidate_digest"] == data["candidate_digest"])
            seen.add(key)
            _require(
                sample["execution_state"] in ("not_started", "started", "finished", "imported")
            )
            _require(
                sample["scoring_state"]
                in ("not_started", "started", "finished", "failed", "interrupted")
            )
            _disposition(sample["execution"])
            _disposition(sample["evaluation"])
            execution = sample["execution"]
            _require(execution is None or execution["outcome"] != "scorer_failed")
            scoring = sample["scoring_state"]
            evaluation = sample["evaluation"]
            _require(
                (sample["execution_state"] in ("finished", "imported")) == (execution is not None)
            )
            if sample["execution_state"] == "imported":
                # A carried pair is a scored row: a scoreable outcome, scored.
                _require(execution["kind"] == "score" and scoring == "finished")
            if execution is None:
                _require(scoring == "not_started" and evaluation is None)
            elif execution["kind"] != "score":
                _require(scoring == "not_started" and evaluation == execution)
            elif scoring in ("not_started", "started"):
                _require(evaluation is None)
            elif scoring == "finished":
                _require(evaluation == execution)
            else:
                from evolution.tree.execution.sample_outcomes import disposition_for

                _require(
                    evaluation
                    == asdict(
                        disposition_for("scorer_failed" if scoring == "failed" else "interrupted")
                    )
                )
            if scoring == "finished":
                _require(type(sample["metrics"]) is dict and bool(sample["metrics"]))
                for name, value in sample["metrics"].items():
                    _identifier(name)
                    _require(type(value) in (float, int))
                _require(sample["critical_pass"] is None or type(sample["critical_pass"]) is bool)
            else:
                _require(sample["metrics"] is None and sample["critical_pass"] is None)
    if kind == "status":
        _require(type(data["recovery_actions"]) is list and type(data["owned_instances"]) is list)
        if data["lower_bound"]:
            _require(not data["recovery_actions"])
        for action in data["recovery_actions"]:
            _keys(action, {"event", "operation_id"})
            _identifier(action["operation_id"])
            _require(
                action["event"]
                in ("operation_interrupted", "rollout_interrupted", "scoring_interrupted")
            )
        for instance in data["owned_instances"]:
            _keys(instance, {"instance_id", "candidate_digest", "strategy"})
            _identifier(instance["instance_id"])
            _identifier(instance["candidate_digest"])
            _require(
                instance["strategy"]
                in ("adapter_apply_manifest", "filesystem_overlay", "artifact_root_injection")
            )
    if kind == "result":
        _require(data["claim_scope"] == "rollout_measurement")
        for name in ("campaign_outcome", "selection", "initial_champion", "final_champion"):
            _require(data[name] is None)


@dataclass(frozen=True, init=False)
class Projection:
    _canonical: bytes

    def __init__(self, payload: Mapping[str, Any], *, secrets: Sequence[str] = ()):
        try:
            raw = canonical_json(payload)
            data = json.loads(raw)
            _validate(data)
            if any(
                type(secret) is not str or (secret and secret in raw.decode()) for secret in secrets
            ):
                raise SchemaError("sensitive public projection")
        except SchemaError:
            raise
        except (TypeError, ValueError, KeyError, AttributeError):
            raise SchemaError("invalid public projection") from None
        object.__setattr__(self, "_canonical", raw)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, secrets: Sequence[str] = ()) -> Projection:
        return cls(payload, secrets=secrets)

    @classmethod
    def from_bytes(cls, raw: bytes, *, secrets: Sequence[str] = ()) -> Projection:
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result

        try:
            return cls(json.loads(raw, object_pairs_hook=unique), secrets=secrets)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise SchemaError("invalid public projection") from None

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    def encode(self) -> bytes:
        return self._canonical + b"\n"


def _project(snapshot: Snapshot, kind: str, secrets: Sequence[str]) -> Projection:
    plan = recovery_plan(snapshot.records)
    first = snapshot.records[0]
    run_id = first.to_payload()["run_id"]
    identity = first.payload["identity"]
    data = dict(
        schema_version=1,
        kind=kind,
        run_id=run_id,
        run_identity_digest=first.payload["identity_digest"],
        candidate_digest=identity["candidate_digest"],
        last_sequence=snapshot.sequence,
        prefix_digest=snapshot.prefix_digest,
        valid_bytes=snapshot.valid_bytes,
        consistency=snapshot.consistency,
        complete=snapshot.complete,
        journal_integrity=snapshot.journal_integrity,
        diagnostics=list(snapshot.diagnostics),
        lower_bound=not snapshot.complete,
        references=[
            dict(
                version=1,
                kind="journal_record",
                run_id=run_id,
                locator=f"events.jsonl#bytes=0:{snapshot.valid_bytes}",
                content_digest=snapshot.prefix_digest,
            )
        ],
    )
    if kind == "events":
        data["events"] = [record.to_payload() for record in snapshot.records]
    else:
        starts: dict[tuple[str, str], int] = {}
        scores = {}
        for record in snapshot.records:
            p = record.payload
            if record.event == "rollout_started":
                key = (p["case_id"], p["sample_id"])
                starts[key] = starts.get(key, 0) + 1
            if record.event in ("scoring_finished", "sample_imported"):
                scores[(p["case_id"], p["sample_id"])] = p
        samples = []
        for decision in plan.executions:
            key = (decision.case_id, decision.sample_id)
            score = scores.get(key)
            samples.append(
                dict(
                    case_id=decision.case_id,
                    sample_id=decision.sample_id,
                    candidate_digest=decision.candidate_digest,
                    execution_state="imported"
                    if decision.imported
                    else "finished"
                    if decision.execution
                    else "started"
                    if key in starts
                    else "not_started",
                    execution=asdict(decision.execution) if decision.execution else None,
                    scoring_state=decision.scoring_state,
                    evaluation=asdict(decision.evaluation) if decision.evaluation else None,
                    metrics=score["metrics"] if score else None,
                    critical_pass=score["critical_pass"] if score else None,
                )
            )
            if "sample_retries" in identity:
                samples[-1]["tries"] = starts.get(key, 0)
        data["samples"] = samples
    if kind == "status":
        data["recovery_actions"] = (
            [dict(event=action.event, operation_id=action.operation_id) for action in plan.actions]
            if snapshot.complete
            else []
        )
        data["owned_instances"] = [asdict(instance) for instance in plan.cleanup]
    elif kind == "result":
        data.update(
            claim_scope="rollout_measurement",
            campaign_outcome=None,
            selection=None,
            initial_champion=None,
            final_champion=None,
        )
    return Projection(data, secrets=secrets)


def project_status(snapshot: Snapshot, *, secrets: Sequence[str] = ()) -> Projection:
    return _project(snapshot, "status", secrets)


def project_events(snapshot: Snapshot, *, secrets: Sequence[str] = ()) -> Projection:
    return _project(snapshot, "events", secrets)


def project_result(snapshot: Snapshot, *, secrets: Sequence[str] = ()) -> Projection:
    return _project(snapshot, "result", secrets)
