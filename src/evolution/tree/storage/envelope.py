"""Closed journal envelope grammar shared by every journal family."""

from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
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
    """A closed event vocabulary: payload rules, operationless events, start predicate.

    `pure` declares that `validate_payload` depends on nothing but its
    arguments: no files, clock, environment, locale or mutable module state.
    Only then may `Envelope.from_bytes` reuse the admission it reached earlier
    in this process for the exact same bytes, secrets and family. A family
    whose rules ever need outside state must drop the flag.
    """

    validate_payload: Callable[[str, dict[str, Any]], None]
    no_operation_events: frozenset[str]
    is_start: Callable[[str], bool]
    pure: bool = False


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
    #: The family `from_payload` admitted these bytes under. Only
    #: `from_payload` sets it, after every check passed, and the class has no
    #: constructor, so record data cannot forge it; see `validated_under`.
    _family: EnvelopeFamily | None = field(default=None, compare=False, repr=False)

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
            object.__setattr__(result, "_family", family)
            return result
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise SchemaError("invalid journal record") from None

    @classmethod
    def from_bytes(
        cls, raw: bytes, *, secrets: Sequence[str] = (), family: EnvelopeFamily
    ) -> Envelope:
        key = _memo_key(raw, secrets, family) if cls is Envelope else None
        if key is not None:
            hit = _memo_get(key)
            # An equal copy of the family finds the key; only the same object
            # may reuse the admission, as `validated_under` promises.
            if hit is not None and hit._family is family:
                return hit
        try:
            value = json.loads(raw, object_pairs_hook=_pairs)
        except (ValueError, TypeError, RecursionError):
            raise SchemaError("invalid journal record") from None
        result = cls.from_payload(value, secrets=secrets, family=family)
        if key is not None:
            _memo_put(key, result)
        return result

    def validated_under(self, family: EnvelopeFamily) -> bool:
        """Did `from_payload` admit these bytes under exactly `family`?

        The bytes are immutable and a family's admission depends on them, the
        family and the secrets checked. A caller that would repeat the
        admission under the same family with no more secrets may rely on this
        instead. Any other family, an equal copy included, says no.
        """
        return self._family is family

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


# Process-local memo of `from_bytes` admissions for pure families. Journals are
# re-read on every replay, resume and audit, and each read admitted every
# record again with the same result. Only admissions are kept: a refused record
# is parsed and refused again, with the same error. The key is the exact record
# bytes, the family object and the secrets, never a path, inode or mtime, so an
# audit still reads its bytes from disk and any changed byte misses. Bounded by
# bytes and evicted least recently used.
_MemoKey = tuple[bytes, EnvelopeFamily, tuple[str, ...]]
_MEMO_MAX_BYTES = 32 * 1024 * 1024
_MEMO_MAX_RECORD = 1024 * 1024
_MEMO_ENTRY_OVERHEAD = 256
_memo: OrderedDict[_MemoKey, Envelope] = OrderedDict()
_memo_size = 0
_memo_lock = threading.Lock()


def _memo_key(raw: object, secrets: Sequence[str], family: EnvelopeFamily) -> _MemoKey | None:
    if family.pure is not True or type(raw) is not bytes or len(raw) > _MEMO_MAX_RECORD:
        return None
    frozen = tuple(secrets)
    if not all(type(secret) is str for secret in frozen):
        return None
    return raw, family, frozen


def _memo_cost(key: _MemoKey, value: Envelope) -> int:
    return len(key[0]) + len(value._encoded) + sum(map(len, key[2])) + _MEMO_ENTRY_OVERHEAD


def _memo_get(key: _MemoKey) -> Envelope | None:
    with _memo_lock:
        hit = _memo.get(key)
        if hit is not None:
            _memo.move_to_end(key)
        return hit


def _memo_put(key: _MemoKey, value: Envelope) -> None:
    global _memo_size
    cost = _memo_cost(key, value)
    with _memo_lock:
        if key in _memo:
            return
        _memo[key] = value
        _memo_size += cost
        while _memo_size > _MEMO_MAX_BYTES and _memo:
            old_key, old_value = _memo.popitem(last=False)
            _memo_size -= _memo_cost(old_key, old_value)


def clear_envelope_memo() -> None:
    """Forget every reused admission (for tests that replace a family's rules)."""
    global _memo_size
    with _memo_lock:
        _memo.clear()
        _memo_size = 0


def _after_fork_in_child() -> None:
    # A fork while another thread held the lock would leave the child's copy
    # locked forever; the child starts with a fresh lock and an empty memo.
    global _memo_lock, _memo_size
    _memo_lock = threading.Lock()
    _memo.clear()
    _memo_size = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


@dataclass(frozen=True)
class LifecycleTransition:
    """Validated bounded delta; commit once while the owner holds its mutex."""

    reserved_records: int
    _apply: Callable[[], None]

    def commit(self) -> None:
        self._apply()
