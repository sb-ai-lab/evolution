"""Strict, framework-neutral codecs for the closed service /v1 request shapes."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from evolution_lab.owner import Accepted, OwnerError, ResourceRef, TargetRef, TaskRef
from evolution_lab.owner.models import Approval, MEASUREMENT_ID, CORE_ID, ATTEMPT_ID, REFLECTION_ID
from evolution_lab.preparation import (
    CampaignSource,
    EnvironmentInput,
    EvaluationSource,
    PathBinding,
    SourceRoot,
)
from evolution_lab.preparation.models import (
    PreparationError, RuntimePolicy, UNMEASURED_LIMIT_DETAIL, _integer, parse_call_retries,
    parse_max_consecutive_unmeasured, parse_min_coverage, parse_sample_retries,
)

from .errors import ServiceError


BODY_LIMIT = 1024 * 1024
PREPARE_REQUEST_SCHEMA = "lab-service-prepare-request/5"
#: /2 has no split; /3 adds split; /4 adds settings; /5 adds the Lab stop threshold.
PREPARE_REQUEST_SCHEMAS = frozenset({
    "lab-service-prepare-request/2", "lab-service-prepare-request/3", "lab-service-prepare-request/4",
    PREPARE_REQUEST_SCHEMA,
})


@dataclass(frozen=True, slots=True)
class BundleRequest:
    source: CampaignSource | EvaluationSource
    environments: tuple[EnvironmentInput | ServiceEnvironmentMarker, ...]
    env: Mapping[str, str]
    secret_names: tuple[str, ...]
    roots: tuple[SourceRoot, ...]
    path_bindings: tuple[PathBinding, ...]


@dataclass(frozen=True, slots=True)
class PrepareRequest:
    kind: str
    bundle_digest: str
    kwargs: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TargetStartRequest:
    secrets: Mapping[str, str]
    execution_policy: str | None = None


@dataclass(frozen=True, slots=True)
class CheckRequest:
    target_ref: TargetRef


@dataclass(frozen=True, slots=True)
class ConformanceRequest:
    target_ref: TargetRef
    runtime_policy: RuntimePolicy | None


@dataclass(frozen=True, slots=True)
class OperationRequest:
    prepared_digest: str
    target_ref: TargetRef
    secrets: Mapping[str, str]
    linked_from: str | None = None
    baseline: str = 'inherit'
    seed: str = 'champion'
    baseline_from: str | None = None
    approval: Approval | None = None


@dataclass(frozen=True, slots=True)
class TraceRequest:
    out: Path
    secrets: Mapping[str, str]
    disclose_private: bool
    measurements: tuple[str, ...]
    max_field_bytes: int


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    target_ref: TargetRef
    secrets: Mapping[str, str]
    accept: tuple[Accepted, ...] = ()
    reason: str | None = None
    prepared_digest: str | None = None
    approval: Approval | None = None
    observed_start: int | None = None


@dataclass(frozen=True, slots=True)
class DeleteResultsRequest:
    approval: Approval | None
    observed_start: int


@dataclass(frozen=True, slots=True)
class CleanupRequest:
    ref: ResourceRef
    wait_seconds: int | float | None
    approval: Approval | None = None
    observed_start: int | None = None


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    ref: ResourceRef
    reason: str
    approval: Approval | None = None
    observed_start: int | None = None


@dataclass(frozen=True, slots=True)
class PolicyConfirmRequest:
    policy: Mapping[str, Any]
    expected_digest: str
    author: str
    channel: str
    note: str | None
    approval: Approval | None = None


def decode_policy_confirm_request(payload: Any) -> PolicyConfirmRequest:
    keys = {"schema", "policy", "expected_digest", "author", "channel", "note"}
    if isinstance(payload, Mapping) and payload.get("schema") == "lab-service-policy-confirm-request/2":
        row = _closed(payload, keys | {"approval"})
    else:
        row = _closed(payload, keys, schema="lab-service-policy-confirm-request/1")
    if not isinstance(row["policy"], dict):
        _invalid()
    if row["policy"].get("schema") == "lab-execution-policy/2":
        from evolution_lab.execution_policy import PolicyInvalid, parse_policy, safe_policy_pointer
        try:
            parse_policy(row["policy"])
        except PolicyInvalid as error:
            pointer = safe_policy_pointer(error.path)
            raise ServiceError("invalid_request", detail=f"field: {pointer}" if pointer else None) from None
    return PolicyConfirmRequest(row["policy"], _digest(row["expected_digest"]),
                                _string(row["author"]), _string(row["channel"]),
                                None if row["note"] is None else _string(row["note"], empty=True),
                                _approval(row))


def _approval(row: Mapping[str, Any]) -> Approval | None:
    if "approval" not in row:
        return None
    try:
        return Approval.decode(row["approval"])
    except ValueError:
        raise ServiceError("invalid_request") from None


def _observed_start(value: Any) -> int:
    if type(value) is not int or value < 1:
        _invalid()
    return value


def _invalid() -> None:
    raise ServiceError("invalid_request")


def _unsupported_schema() -> None:
    raise ServiceError("unsupported_schema")


def _reject_constant(_: str) -> None:
    _invalid()


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _finite_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        _invalid()
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    return value


def decode_json_object(raw: bytes, *, max_bytes: int | None = BODY_LIMIT) -> dict[str, Any]:
    """Decode a CLI/file JSON object without HTTP media validation."""
    if max_bytes is not None and len(raw) > max_bytes:
        raise ServiceError("request_too_large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs, parse_constant=_reject_constant)
    except (RecursionError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        _invalid()
    try:
        value = _finite_json(value)
    except RecursionError:
        _invalid()
    if not isinstance(value, dict):
        _invalid()
    return value


def decode_http_json_object(raw: bytes, *, content_type: str | None,
                            max_bytes: int | None = BODY_LIMIT) -> dict[str, Any]:
    """Decode an HTTP body after requiring its explicit JSON media type."""
    if not isinstance(content_type, str) or content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise ServiceError("unsupported_media_type")
    return decode_json_object(raw, max_bytes=max_bytes)


def _closed(payload: Any, keys: set[str], *, schema: str | None = None) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        _invalid()
    if set(payload) != keys:
        missing = sorted(keys - set(payload))
        extra = len(set(payload) - keys)
        parts = ([f"missing: {', '.join(missing)}"] if missing else []) + \
                ([f"unexpected: {extra} field(s)"] if extra else [])
        raise ServiceError("invalid_request", detail="; ".join(parts))
    if schema is not None and payload.get("schema") != schema:
        _unsupported_schema()
    return payload


def _string(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        _invalid()
    return value


def _digest(value: Any) -> str:
    value = _string(value)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        _invalid()
    return value


def decode_digest(value: Any) -> str:
    """Decode a public content digest used in a route path or request body."""
    return _digest(value)


def decode_identifier(value: Any) -> str:
    """Decode a public Owner identifier without reaching into its private codec."""
    try:
        return ResourceRef("operation", _string(value)).id
    except (OwnerError, ValueError, TypeError):
        _invalid()


def decode_measurement_id(value: Any) -> str:
    """A measurement id, optionally with core's ``@e<k>`` epoch (spec 2026-09-30 §2.5)."""
    text = _string(value)
    if MEASUREMENT_ID.match(text) is None:
        _invalid()
    return text


def _content_id(value: Any, pattern, field: str) -> str:
    text = _string(value)
    if pattern.fullmatch(text) is None:
        raise ServiceError("invalid_request", detail=f"field: {field}")
    return text


def decode_core_id(value: Any, field: str) -> str:
    """ASGI already decoded once; '%' is outside core's grammar."""
    return _content_id(value, CORE_ID, field)


def decode_attempt_id(value: Any) -> str:
    return _content_id(value, ATTEMPT_ID, "attempt_id")


def decode_reflection_id(value: Any) -> str:
    return _content_id(value, REFLECTION_ID, "reflection_id")


def decode_disclose(params: Mapping[str, str]) -> bool:
    value = params.get("disclose")
    if value not in (None, "private"):
        raise ServiceError("invalid_request", detail="field: disclose")
    return value == "private"


def _positive_number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid()
    try:
        valid = math.isfinite(value) and value > 0
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        _invalid()
    return value


def _absolute_path(value: Any) -> Path:
    path = Path(_string(value))
    if not path.is_absolute():
        _invalid()
    return path


def _path_under(base: Path | None):
    """Absolute paths only without ``base``; with it, relative paths resolve from ``base``."""
    if base is None:
        return _absolute_path

    def resolve(value: Any) -> Path:
        text = _string(value)
        if "\x00" in text or "\\" in text:
            _invalid()
        path = Path(text)
        return path if path.is_absolute() else Path(os.path.normpath(base / path))
    return resolve


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        _invalid()
    return {_string(key): _string(item, empty=True) for key, item in value.items()}


def _unique_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        _invalid()
    result = tuple(_string(item) for item in value)
    if len(set(result)) != len(result):
        _invalid()
    return result


def _source(value: Any, *, path=_absolute_path) -> CampaignSource | EvaluationSource:
    if not isinstance(value, Mapping):
        _invalid()
    kind = value.get("kind")
    if kind == "campaign":
        row = _closed(value, {"kind", "manifest_path", "launch_path"})
        return CampaignSource(path(row["manifest_path"]), path(row["launch_path"]))
    if kind == "evaluation":
        row = _closed(value, {"kind", "input_path", "launch_path"})
        return EvaluationSource(path(row["input_path"]), path(row["launch_path"]))
    _invalid()


BUNDLE_REQUEST_SCHEMAS = ("lab-service-bundle-request/1", "lab-service-bundle-request/2")


@dataclass(frozen=True, slots=True)
class ServiceEnvironmentMarker:
    """``{"kind": "service"}``: the route expands it into the service environment."""


SERVICE_ENVIRONMENT = ServiceEnvironmentMarker()


def _environments(value: Any, *, allow_service: bool, path=_absolute_path) -> tuple[EnvironmentInput | ServiceEnvironmentMarker, ...]:
    if not isinstance(value, list):
        _invalid()
    result = []
    for index, item in enumerate(value):
        if isinstance(item, Mapping) and "kind" in item:
            # Preparation requires the execution environment first and exactly once.
            if not allow_service or index != 0 or dict(item) != {"kind": "service"}:
                _invalid()
            result.append(SERVICE_ENVIRONMENT)
            continue
        row = _closed(item, {"executable", "wheel_paths", "runtime_files"})
        if not isinstance(row["wheel_paths"], list) or not isinstance(row["runtime_files"], list):
            _invalid()
        result.append(EnvironmentInput(
            path(row["executable"]),
            tuple(path(item) for item in row["wheel_paths"]),
            tuple(path(item) for item in row["runtime_files"]),
        ))
    return tuple(result)


def _roots(value: Any, *, path=_absolute_path) -> tuple[SourceRoot, ...]:
    if not isinstance(value, list):
        _invalid()
    rows = []
    aliases = set()
    for item in value:
        row = _closed(item, {"alias", "source_path"})
        alias = _string(row["alias"])
        if alias in aliases:
            _invalid()
        aliases.add(alias)
        try:
            rows.append(SourceRoot(alias, path(row["source_path"])))
        except ValueError:
            _invalid()
    return tuple(rows)


def _path_bindings(value: Any, *, path=_absolute_path) -> tuple[PathBinding, ...]:
    if not isinstance(value, list):
        _invalid()
    rows = []
    for item in value:
        row = _closed(item, {"document", "pointer", "root_alias", "relative_path"})
        try:
            rows.append(PathBinding(
                path(row["document"]), _string(row["pointer"]),
                _string(row["root_alias"]), _string(row["relative_path"]),
            ))
        except ValueError:
            _invalid()
    return tuple(rows)


def decode_bundle_request(payload: Any, *, base: Path | None = None) -> BundleRequest:
    row = _closed(payload, {"schema", "source", "environments", "env", "secret_names", "roots", "path_bindings"})
    if row["schema"] not in BUNDLE_REQUEST_SCHEMAS:
        _unsupported_schema()
    path = _path_under(base)
    return BundleRequest(
        _source(row["source"], path=path),
        _environments(row["environments"], allow_service=row["schema"] == BUNDLE_REQUEST_SCHEMAS[1], path=path),
        _string_map(row["env"]), _unique_strings(row["secret_names"]), _roots(row["roots"], path=path),
        _path_bindings(row["path_bindings"], path=path),
    )


def _runtime_policy(value: Any) -> RuntimePolicy | None:
    if value is None:
        return None
    row = _closed(value, {"stop_grace_seconds", "cleanup_wait_seconds"})
    try:
        return RuntimePolicy(_positive_number(row["stop_grace_seconds"]), _positive_number(row["cleanup_wait_seconds"]))
    except ValueError:
        _invalid()


def decode_runtime_policy(payload: Any) -> RuntimePolicy:
    """Decode the exact public RuntimePolicy object for CLI and routes."""
    policy = _runtime_policy(payload)
    if policy is None:
        _invalid()
    return policy


def decode_secret_map(payload: Any) -> dict[str, str]:
    """Decode an explicit closed string map; no ambient environment merge occurs."""
    return _string_map(payload)


def _selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or len(value) != 1:
        _invalid()
    if set(value) == {"all"} and value["all"] is True:
        return {"all": True}
    if set(value) == {"count"}:
        count = value["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            _invalid()
        return {"count": count}
    if set(value) == {"case_ids"}:
        return {"case_ids": list(_unique_strings(value["case_ids"]))}
    _invalid()


def _call_retries(value: Any) -> Any:
    if value is None or value == "off":
        return value
    try:
        return parse_call_retries(value, "prepare")
    except PreparationError:
        _invalid()


def decode_prepare_request(payload: Any) -> PrepareRequest:
    if not isinstance(payload, Mapping):
        _invalid()
    # The version first: /2 has no split; /4 also carries evaluation settings.
    schema = payload.get("schema")
    if not isinstance(schema, str) or schema not in PREPARE_REQUEST_SCHEMAS:
        _unsupported_schema()
    kind = payload.get("kind")
    if kind == "campaign":
        row = _closed(
            payload, {"schema", "kind", "bundle_digest", "stop_grace_seconds", "cleanup_wait_seconds"},
            schema=schema,
        )
        values = {name: row[name] for name in ("stop_grace_seconds", "cleanup_wait_seconds")}
        if any(value is not None for value in values.values()):
            for value in values.values():
                if value is not None:
                    _positive_number(value)
        return PrepareRequest("campaign", _digest(row["bundle_digest"]), values)
    if kind == "evaluation":
        row = _closed(
            payload,
            {"schema", "kind", "bundle_digest", "selection", "repeat_seeds", "evaluation_set_id", "max_turns",
             "timeout_seconds", "metric_plan", "runtime_policy", "max_failed_turns", "call_retries"}
            | ({"split"} if schema != "lab-service-prepare-request/2" else set())
            | ({"sample_retries", "min_coverage", "concurrency", "scorer_concurrency"}
               if schema in {"lab-service-prepare-request/4", PREPARE_REQUEST_SCHEMA} else set())
            | ({"max_consecutive_unmeasured"} if schema == PREPARE_REQUEST_SCHEMA else set()),
            schema=schema,
        )
        seeds = row["repeat_seeds"]
        if seeds is not None:
            if not isinstance(seeds, list) or any(seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)) for seed in seeds):
                _invalid()
            seeds = tuple(seeds)
        for name in ("evaluation_set_id",):
            if row[name] is not None:
                _string(row[name])
        if row["max_turns"] is not None and (isinstance(row["max_turns"], bool) or not isinstance(row["max_turns"], int) or row["max_turns"] < 1):
            _invalid()
        if row["timeout_seconds"] is not None:
            _positive_number(row["timeout_seconds"])
        metric_plan = row["metric_plan"]
        if metric_plan is not None and not isinstance(metric_plan, Mapping):
            _invalid()
        if row["max_failed_turns"] is not None and (isinstance(row["max_failed_turns"], bool)
                or not isinstance(row["max_failed_turns"], int) or row["max_failed_turns"] < 1):
            _invalid()
        call_retries = _call_retries(row["call_retries"])
        split = row.get("split")  # absent in /2: null, train
        if split not in (None, "train", "test"):
            _invalid()
        settings = {}
        if schema in {"lab-service-prepare-request/4", PREPARE_REQUEST_SCHEMA}:
            try:
                settings = {
                    "sample_retries": parse_sample_retries(row["sample_retries"], "prepare"),
                    "min_coverage": parse_min_coverage(row["min_coverage"], "prepare"),
                    **{name: None if row[name] is None else _integer(row[name], "prepare", minimum=1)
                       for name in ("concurrency", "scorer_concurrency")},
                }
            except PreparationError:
                _invalid()
        if schema == PREPARE_REQUEST_SCHEMA:
            try:
                parse_max_consecutive_unmeasured(row["max_consecutive_unmeasured"], "prepare")
            except PreparationError:
                raise ServiceError("invalid_request", detail=UNMEASURED_LIMIT_DETAIL) from None
            # Null is not an API override of evaluation-input defaults.
            settings["max_consecutive_unmeasured"] = row["max_consecutive_unmeasured"]
        return PrepareRequest("evaluation", _digest(row["bundle_digest"]), {
            "selection": _selection(row["selection"]), "repeat_seeds": seeds,
            "evaluation_set_id": row["evaluation_set_id"], "max_turns": row["max_turns"],
            "timeout_seconds": row["timeout_seconds"], "metric_plan": metric_plan,
            "runtime_policy": _runtime_policy(row["runtime_policy"]),
            "max_failed_turns": row["max_failed_turns"], "call_retries": call_retries, "split": split,
            **settings,
        })
    _invalid()


def decode_bind_request(payload: Any) -> str:
    """Decode POST /connections/from-bundle without exposing a generic dispatcher."""
    row = _closed(payload, {"schema", "bundle_digest"}, schema="lab-service-bind-request/1")
    return _digest(row["bundle_digest"])


def decode_resource_ref(payload: Any) -> ResourceRef:
    try:
        row = _closed(payload, {"kind", "id"})
        return ResourceRef(_string(row["kind"]), _string(row["id"]))
    except (OwnerError, ValueError, TypeError):
        _invalid()


def decode_target_ref(payload: Any) -> TargetRef:
    try:
        row = _closed(payload, {"schema", "id", "owner_generation"}, schema="lab-target-ref/1")
        return TargetRef(_string(row["id"]), _string(row["owner_generation"]))
    except (OwnerError, ValueError, TypeError):
        _invalid()


def decode_task_ref(payload: Any) -> TaskRef:
    try:
        row = _closed(payload, {"schema", "id", "owner_generation"}, schema="lab-task-ref/1")
        return TaskRef(_string(row["id"]), _string(row["owner_generation"]))
    except (OwnerError, ValueError, TypeError):
        _invalid()


def decode_target_query(target_id: Any, owner_generation: Any) -> TargetRef:
    """Construct the required full target ref from the closed route/query pair."""
    try:
        return TargetRef(_string(target_id), _string(owner_generation))
    except (OwnerError, ValueError, TypeError):
        _invalid()


def decode_task_query(task_id: Any, owner_generation: Any) -> TaskRef:
    """Construct the required full task ref from the closed route/query pair."""
    try:
        return TaskRef(_string(task_id), _string(owner_generation))
    except (OwnerError, ValueError, TypeError):
        _invalid()


def decode_empty_request(payload: Any) -> None:
    _closed(payload, set())


def decode_target_start_request(payload: Any) -> TargetStartRequest:
    if isinstance(payload, Mapping) and payload.get("schema") == "lab-service-target-start-request/2":
        row = _closed(payload, {"schema", "secrets", "execution_policy"}, schema="lab-service-target-start-request/2")
        digest = None if row["execution_policy"] is None else _digest(row["execution_policy"])
        return TargetStartRequest(_string_map(row["secrets"]), digest)
    row = _closed(payload, {"schema", "secrets"}, schema="lab-service-target-start-request/1")
    return TargetStartRequest(_string_map(row["secrets"]))


def decode_check_request(payload: Any) -> CheckRequest:
    row = _closed(payload, {"schema", "target_ref"}, schema="lab-service-check-request/1")
    return CheckRequest(decode_target_ref(row["target_ref"]))


def decode_conformance_request(payload: Any) -> ConformanceRequest:
    row = _closed(payload, {"schema", "target_ref", "runtime_policy"}, schema="lab-service-conformance-request/1")
    return ConformanceRequest(decode_target_ref(row["target_ref"]), _runtime_policy(row["runtime_policy"]))


def decode_operation_request(payload: Any) -> OperationRequest:
    schema = payload.get("schema") if isinstance(payload, Mapping) else None
    if schema in ("lab-service-start-request/2", "lab-service-start-request/3", "lab-service-start-request/4"):
        keys = {"schema", "prepared_digest", "target_ref", "secrets", "linked_from", "baseline", "seed"}
        if schema in ("lab-service-start-request/3", "lab-service-start-request/4"):
            keys.add("baseline_from")
        if schema == "lab-service-start-request/4":
            keys.add("approval")
        row = _closed(payload, keys)
        baseline, seed = _string(row["baseline"]), _string(row["seed"])
        if baseline not in {"inherit", "fresh"} or seed not in {"champion", "project"}:
            _invalid()
        linked_from = None if row["linked_from"] is None else decode_identifier(row["linked_from"])
        baseline_from = None if row.get("baseline_from") is None else decode_identifier(row["baseline_from"])
        if baseline_from is not None and (linked_from is not None or (baseline, seed) != ("inherit", "champion")):
            _invalid()  # baseline and seed shape a linked start only
        return OperationRequest(_digest(row["prepared_digest"]), decode_target_ref(row["target_ref"]),
            _string_map(row["secrets"]), linked_from, baseline, seed, baseline_from, _approval(row))
    row = _closed(payload, {"schema", "prepared_digest", "target_ref", "secrets"}, schema="lab-service-start-request/1")
    return OperationRequest(_digest(row["prepared_digest"]), decode_target_ref(row["target_ref"]), _string_map(row["secrets"]))


def decode_trace_request(payload: Any) -> TraceRequest:
    row = _closed(payload, {'schema', 'out', 'secrets', 'disclose_private', 'measurements', 'max_field_bytes'},
                  schema='lab-service-trace-request/1')
    if (type(row['disclose_private']) is not bool or not isinstance(row['measurements'], list)
            or type(row['max_field_bytes']) is not int or row['max_field_bytes'] < 1):
        _invalid()
    return TraceRequest(_absolute_path(row['out']), _string_map(row['secrets']), row['disclose_private'],
                        tuple(decode_measurement_id(item) for item in row['measurements']), row['max_field_bytes'])


def decode_resume_request(payload: Any) -> ResumeRequest:
    schema = payload.get('schema') if isinstance(payload, Mapping) else None
    if schema in ('lab-service-resume-request/2', 'lab-service-resume-request/3'):
        keys = {'schema', 'target_ref', 'secrets', 'accept', 'reason', 'prepared_digest'}
        observed_start = None
        if schema == 'lab-service-resume-request/3':
            keys.update({'approval', 'observed_start'})
        row = _closed(payload, keys)
        if 'observed_start' in row:
            observed_start = _observed_start(row['observed_start'])
        if not isinstance(row['accept'], list):
            _invalid()
        accepted = []
        for item in row['accept']:
            item = _closed(item, {'code', 'digest', 'effect'})
            accepted.append(Accepted(_string(item['code']), _digest(item['digest']),
                                    None if item['effect'] is None else _string(item['effect'])))
        return ResumeRequest(decode_target_ref(row['target_ref']), _string_map(row['secrets']), tuple(accepted),
            None if row['reason'] is None else _string(row['reason'], empty=True),
            None if row['prepared_digest'] is None else _digest(row['prepared_digest']),
            _approval(row), observed_start)
    row = _closed(payload, {"schema", "target_ref", "secrets"}, schema="lab-service-resume-request/1")
    return ResumeRequest(decode_target_ref(row["target_ref"]), _string_map(row["secrets"]))


def decode_delete_results_request(payload: Any) -> DeleteResultsRequest:
    row = _closed(payload, {"schema", "approval", "observed_start"}, schema="lab-service-delete-results-request/1")
    return DeleteResultsRequest(_approval(row), _observed_start(row["observed_start"]))


def _resource_observed_start(row: Mapping[str, Any], ref: ResourceRef) -> int | None:
    start = row["observed_start"]
    if start is None and ref.kind != "operation":
        return None
    return _observed_start(start)


def decode_cleanup_request(payload: Any) -> CleanupRequest:
    keys = {"schema", "ref", "wait_seconds"}
    if isinstance(payload, Mapping) and payload.get("schema") == "lab-service-cleanup-request/2":
        row = _closed(payload, keys | {"approval", "observed_start"})
    else:
        row = _closed(payload, keys, schema="lab-service-cleanup-request/1")
    wait_seconds = row["wait_seconds"]
    if wait_seconds is not None:
        _positive_number(wait_seconds)
    ref = decode_resource_ref(row["ref"])
    if "approval" in row:
        return CleanupRequest(ref, wait_seconds, _approval(row), _resource_observed_start(row, ref))
    return CleanupRequest(ref, wait_seconds)


def decode_release_request(payload: Any) -> ReleaseRequest:
    keys = {"schema", "ref", "reason"}
    if isinstance(payload, Mapping) and payload.get("schema") == "lab-service-release-request/2":
        row = _closed(payload, keys | {"approval", "observed_start"})
        ref = decode_resource_ref(row["ref"])
        return ReleaseRequest(ref, _string(row["reason"]), _approval(row), _resource_observed_start(row, ref))
    row = _closed(payload, keys, schema="lab-service-release-request/1")
    return ReleaseRequest(decode_resource_ref(row["ref"]), _string(row["reason"]))


def serialize_public(value: Any) -> dict[str, Any]:
    """Explicit public projections; no generic dataclass serialization."""
    from evolution.tree import TraceSummary
    if isinstance(value, TraceSummary):
        from evolution_lab.owner.traces import trace_summary_payload
        return trace_summary_payload(value)
    to_payload = getattr(value, "to_payload", None)
    if not callable(to_payload):
        _invalid()
    try:
        payload = to_payload()
    except Exception:
        _invalid()
    if not isinstance(payload, dict):
        _invalid()
    return payload
