"""Closed journal envelope grammar shared by every journal family."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any

IDENTIFIER = re.compile(r"[\w][\w.:@+\-]{0,255}\Z", re.UNICODE)


class SchemaError(ValueError):
    """Safe schema diagnostics never include offending input."""


def _require(ok: bool) -> None:
    if not ok:
        raise SchemaError("invalid journal record")


def is_identifier(value: object) -> bool:
    """The bounded grammar shared by all journal identifiers."""
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _identifier(value: object) -> None:
    _require(is_identifier(value))


def _integer(value: object, minimum: int = 0) -> None:
    _require(type(value) is int and value >= minimum)


def _keys(
    value: object,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    _require(isinstance(value, dict))
    assert isinstance(value, dict)
    _require(required <= value.keys() <= required | optional)
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _finite(value: object) -> None:
    if isinstance(value, float):
        _require(isfinite(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            _require(isinstance(key, str))
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)
    else:
        _require(value is None or type(value) in (str, int, bool))


def _canonical(value: object) -> bytes:
    _finite(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True)
class EnvelopeFamily:
    """A closed event vocabulary: payload rules, operationless events, start predicate."""

    validate_payload: Callable[[str, dict[str, Any]], None]
    no_operation_events: frozenset[str]
    is_start: Callable[[str], bool]


@dataclass(frozen=True)
class SecretMatch:
    """Where a run secret matched and which one, never the secret or the text."""

    location: str
    index: int


def secret_match(value: Mapping[str, Any], secrets: Sequence[str]) -> SecretMatch | None:
    """The one secret predicate: the journal, refusals and probes all ask this.

    `location` is the public grammar: `record-key` for a key name,
    `envelope.<field>` for a string envelope field, otherwise the
    payload-relative path. Keys are checked before values at every level, so a
    key collision is `record-key` even when a value under it would also match.
    `index` is the lowest index in `secrets` of any secret that matches
    anywhere in `value`; callers pass the run's secrets in role order, so it
    names the first role. `location` is the first match walked, which can be a
    different secret's: a caller naming the place of the `index` secret asks
    again with that secret alone. An empty secret never matches. A match is
    decided by the secrets found, not by `location`, whose payload-relative
    path is empty for a payload-root key `""`.
    """

    needles = [(index, secret) for index, secret in enumerate(secrets) if secret]
    lowest: list[int] = []

    def hit(text: str) -> bool:
        found = [index for index, secret in needles if secret in text]
        lowest.extend(found)
        return bool(found)

    def walk(item: Any, path: str) -> str | None:
        first: str | None = None
        if isinstance(item, str):
            return path if hit(item) else None
        if isinstance(item, Mapping):
            for key, child in item.items():
                if hit(str(key)) and first is None:
                    first = "record-key"
                found = walk(child, f"{path}.{key}" if path else str(key))
                if first is None:
                    first = found
        elif isinstance(item, (list, tuple)):
            for position, child in enumerate(item):
                found = walk(child, f"{path}[{position}]")
                if first is None:
                    first = found
        return first

    location: str | None = None
    for key, child in value.items():
        if hit(str(key)) and location is None:
            location = "record-key"
        if key != "payload" and isinstance(child, str) and hit(child) and location is None:
            location = f"envelope.{key}"
    found = walk(value.get("payload", {}), "")
    if location is None:
        location = found
    if not lowest:
        return None
    # Every hit sets a location the first time, so one is set whenever a secret matched.
    return SecretMatch(location="" if location is None else location, index=min(lowest))


@dataclass(frozen=True, init=False)
class Envelope:
    """Immutable canonical bytes prevent mutable aliases bypassing validation.

    `event` and `operation_id` are immutable strings, kept beside the bytes
    when the envelope is built so a reader that filters a journal by event
    does not decode every record again (#95). They are derived from
    `_encoded`, so they take no part in equality, hashing or `repr`.
    `payload` still decodes a fresh copy on every access: callers own the
    dict it returns.
    """

    _encoded: bytes
    _event: str = field(default="", compare=False, repr=False)
    _operation_id: str | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_payload(
        cls, value: object, *, secrets: Sequence[str] = (), family: EnvelopeFamily
    ) -> Envelope:
        try:
            encoded = _canonical(value)
            value = json.loads(encoded)
            v = _keys(
                value,
                {
                    "schema_version",
                    "run_id",
                    "sequence",
                    "timestamp",
                    "event",
                    "operation_id",
                    "payload",
                },
            )
            _require(type(v["schema_version"]) is int and v["schema_version"] == 1)
            _integer(v["sequence"], 1)
            _identifier(v["run_id"])
            _require(isinstance(v["timestamp"], str) and v["timestamp"].endswith("Z"))
            datetime.fromisoformat(v["timestamp"][:-1] + "+00:00")
            event = v["event"]
            _identifier(event)
            if event in family.no_operation_events:
                _require(v["operation_id"] is None)
            else:
                _identifier(v["operation_id"])
            _require(isinstance(v["payload"], dict))
            family.validate_payload(event, v["payload"])

            # Decoded strings too: canonical JSON escaping must not hide a canary.
            _require(secret_match(v, secrets) is None)
            result = object.__new__(cls)
            object.__setattr__(result, "_encoded", encoded)
            object.__setattr__(result, "_event", str(event))
            object.__setattr__(result, "_operation_id", v["operation_id"])
            return result
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise SchemaError("invalid journal record") from None

    @classmethod
    def from_bytes(
        cls, raw: bytes, *, secrets: Sequence[str] = (), family: EnvelopeFamily
    ) -> Envelope:
        try:
            value = json.loads(raw, object_pairs_hook=_pairs)
        except (ValueError, TypeError, RecursionError):
            raise SchemaError("invalid journal record") from None
        return cls.from_payload(value, secrets=secrets, family=family)

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._encoded)

    def encode(self) -> bytes:
        return self._encoded + b"\n"

    @property
    def event(self) -> str:
        return self._event

    @property
    def operation_id(self) -> str | None:
        return self._operation_id

    @property
    def payload(self) -> dict[str, Any]:
        return self.to_payload()["payload"]


@dataclass(frozen=True)
class LifecycleTransition:
    """Validated bounded delta; commit once while the owner holds its mutex."""

    reserved_records: int
    _apply: Callable[[], None]

    def commit(self) -> None:
        self._apply()
