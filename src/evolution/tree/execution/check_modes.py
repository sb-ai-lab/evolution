"""Closed /16 policy-check declarations, separate from consistency guards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, TypeVar, cast

from evolution.tree.execution.evidence_settings import SettingsError
from evolution.tree.execution.journal_identity import canonical_json

_FIELD: ContextVar[str | None] = ContextVar("check_field", default=None)
_SILENT: ContextVar[bool] = ContextVar("check_serialized_container", default=False)
_DEFERRED: ContextVar[list[Callable[[Any], bool]] | None] = ContextVar(
    "deferred_shadow_checks", default=None
)


def quiet_forbidden(predicate: Callable[[str], bool], text: str) -> bool:
    """Pre-screen optional text without recording or deferring shadow hits."""
    token = _SILENT.set(True)
    pending = _DEFERRED.set(None)
    try:
        return predicate(text)
    finally:
        _DEFERRED.reset(pending)
        _SILENT.reset(token)


@contextmanager
def check_field(location: str | None) -> Iterator[None]:
    """Carry a field through existing single-argument scanner callbacks."""
    token = _FIELD.set(location)
    try:
        yield
    finally:
        _FIELD.reset(token)


def grouping_field_key(members: Sequence[str]) -> str:
    """Stable across projection renumbering, without retaining scanned text."""
    return hashlib.sha256(canonical_json(sorted(members))).hexdigest()


def grouping_field_locations(
    value: Mapping[str, Any], evidence_ids: Sequence[str] = ()
) -> dict[tuple[str, ...], str]:
    locations: dict[tuple[str, ...], str] = {}
    groups = value.get("groups")
    if isinstance(groups, list):
        for index, group in enumerate(groups):
            if not isinstance(group, Mapping):
                continue
            members = group.get("members", ())
            members = members if isinstance(members, list) else []
            ids = [
                m["evidence_id"]
                for m in members
                if isinstance(m, Mapping) and isinstance(m.get("evidence_id"), str)
            ]
            key = grouping_field_key(ids) if ids else f"unknown/{index}"
            for name in ("title", "failure_mode", "expected_behaviour", "question"):
                locations[("groups", str(index), name)] = (
                    f"{'label' if name == 'title' else 'description'}/{key}"
                )
            locations[("groups", str(index), "priority")] = f"priority/{key}"
            citations = group.get("citations", ())
            for position in range(len(citations) if isinstance(citations, list) else 0):
                locations[("groups", str(index), "citations", str(position))] = (
                    f"citation/{key}/{position}"
                )
            for position, member in enumerate(members):
                if isinstance(member, Mapping) and isinstance(member.get("evidence_id"), str):
                    locations[("groups", str(index), "members", str(position), "variant")] = (
                        f"variant/{member['evidence_id']}"
                    )
    else:
        assignments = value.get("assignments", ())
        assignments = assignments if isinstance(assignments, (list, tuple)) else ()
        descriptions = value.get("descriptions", {})
        for number in descriptions if isinstance(descriptions, Mapping) else ():
            members = [
                evidence_ids[i]
                for i, n in enumerate(assignments)
                if str(n) == str(number) and i < len(evidence_ids)
            ]
            key = grouping_field_key(members) if members else str(number)
            for name in ("label", "description"):
                locations[("descriptions", str(number), name)] = f"{name}/{key}"
        variants = value.get("variants", {})
        for index in variants if isinstance(variants, Mapping) else ():
            name = str(index)
            if name.isdecimal() and int(name) < len(evidence_ids):
                name = evidence_ids[int(name)]
            locations[("variants", str(index))] = f"variant/{name}"
    return locations


def _field_values(
    value: Any, locations: Mapping[tuple[str, ...], str], path: tuple[str, ...] = ()
) -> Iterator[tuple[Any, str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), "key/" + "/".join((*path, str(key)))
            yield from _field_values(item, locations, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _field_values(item, locations, (*path, str(index)))
    else:
        yield value, locations.get(path, "field/" + "/".join(path))


def scan_grouping_value(
    predicate: Callable[[Any], bool], value: Any, evidence_ids: Sequence[str] = ()
) -> bool:
    locations = grouping_field_locations(value, evidence_ids) if isinstance(value, Mapping) else {}
    for item, location in _field_values(value, locations):
        with check_field(location):
            if predicate(item):
                return True
    return False


def _string_tokens(text: str) -> Iterator[tuple[str, str]]:
    cursor = 0
    while cursor < len(text):
        start = text.find('"', cursor)
        if start < 0:
            break
        cursor = start + 1
        while cursor < len(text):
            if text[cursor] == "\\":
                if text.startswith("\\\n", cursor):
                    break
                cursor += 2
            elif text[cursor] == '"':
                break
            else:
                cursor += 1
        if cursor >= len(text):
            break
        if text[cursor] == "\\":
            cursor += 2
            continue
        cursor += 1
        raw = text[start:cursor]
        try:
            yield raw, json.loads(raw)
        except ValueError:
            continue


def _primitive_tokens(text: str) -> Iterator[tuple[str, Any]]:
    """Original lexemes in a valid document, in the leaf walk's order."""
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(text):
        if text[cursor] in " \t\r\n{}[],:":
            cursor += 1
            continue
        value, end = decoder.raw_decode(text, cursor)
        yield text[cursor:end], value
        cursor = end


def combine_checks(*predicates: Callable[[Any], bool]) -> Callable[[Any], bool]:
    """Preserve check precedence while exposing shadow-only field scans."""

    def combined(value: Any) -> bool:
        return any(predicate(value) for predicate in predicates)

    cast(Any, combined).shadow_scan = lambda value: any(
        getattr(predicate, "shadow_scan", lambda _: False)(value) for predicate in predicates
    )
    return combined


def scan_grouping_wire(
    predicate: Callable[[str], bool],
    text: str,
    evidence_ids: Sequence[str] = (),
    *,
    decoded_enforcement: bool = True,
    locate: Callable[[Mapping[str, Any]], Mapping[tuple[str, ...], str]] | None = None,
) -> bool:
    """Scan wire and decoded forms at the same field, preserving early refusal.

    ``locate`` names the fields of a decoded object in place of
    `grouping_field_locations`; the family-15 Grouper's portion answer names
    members by reference, which only its session can expand (b16).
    """

    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate JSON key")
        return value

    try:
        value = json.loads(text, object_pairs_hook=unique_fields)
    except (ValueError, RecursionError):
        # Malformed envelopes still receive the existing complete wire/token screen.
        with check_field("wire/raw"):
            if predicate(text):
                return True
        if not decoded_enforcement:
            shadow_scan = getattr(predicate, "shadow_scan", None)
            if shadow_scan is None:
                return False
            predicate = shadow_scan
        for index, (_raw, decoded) in enumerate(_string_tokens(text)):
            with check_field(f"wire/token/{index}"):
                if predicate(decoded):
                    return True
        return False
    deferred: list[Callable[[Any], bool]] = []
    token = _SILENT.set(True)
    pending = _DEFERRED.set(deferred)
    try:
        refused = predicate(text)
    finally:
        _SILENT.reset(token)
        _DEFERRED.reset(pending)
    if refused:
        for scanner in deferred:
            scan_grouping_wire(scanner, text, evidence_ids, locate=locate)
        return True
    shadow_scan = getattr(predicate, "shadow_scan", None)
    locations: Mapping[tuple[str, ...], str] = {}
    if isinstance(value, Mapping):
        locations = (
            grouping_field_locations(value, evidence_ids) if locate is None else locate(value)
        )
    tokens = iter(_primitive_tokens(text))
    for item, location in _field_values(value, locations):
        with check_field(location):
            raw, decoded = next(tokens, ("", item))
            if shadow_scan is not None:
                shadow_scan(raw)
            if isinstance(decoded, str):
                if decoded_enforcement and predicate(decoded):
                    return True
                if not decoded_enforcement and shadow_scan is not None:
                    shadow_scan(decoded)
    return False


CHECKS_VERSION: Final = "checks/1"
Mode = Literal["enforce", "shadow", "off"]
CHECK_NAMES: Final[tuple[str, ...]] = (
    "reflection_oracle_leak",
    "reflection_run_secret",
    "grouping_oracle_leak",
    "grouping_run_secret",
    "seal_oracle_leak",
    "attempt_feedback_screen",
    "history_excerpt_screen",
    "input_run_secret_redaction",
    "connection_description_secret",
    "verbatim_answer_unmatched",
    "secret_oracle_path_unmatched",
    "connection_preflight",
)
INPUT_CHECKS: Final = (
    "input_run_secret_redaction",
    "attempt_feedback_screen",
    "history_excerpt_screen",
)
CODES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "reflection_oracle_leak": frozenset({"oracle_leak"}),
        "reflection_run_secret": frozenset({"run_secret"}),
        "grouping_oracle_leak": frozenset({"oracle_leak"}),
        "grouping_run_secret": frozenset({"secret_detected"}),
        "seal_oracle_leak": frozenset({"oracle_leak"}),
        "attempt_feedback_screen": frozenset({"withheld"}),
        "history_excerpt_screen": frozenset({"excerpt_dropped"}),
        "input_run_secret_redaction": frozenset({"redacted"}),
        "connection_description_secret": frozenset({"connection_description_secret"}),
        "verbatim_answer_unmatched": frozenset({"verbatim_answer_unmatched"}),
        "secret_oracle_path_unmatched": frozenset({"secret_oracle_path_unmatched"}),
        "connection_preflight": frozenset(
            {"connection_preflight_unavailable", "connection_preflight_inconclusive"}
        ),
    }
)

CONSEQUENCES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "reflection_oracle_leak": "an oracle string in a diagnosis can reach the Editor and fit the skill to reference answers",
        "reflection_run_secret": "a run secret in a diagnosis is admitted; the journal and the Editor see it as [REDACTED]",
        "grouping_oracle_leak": "an oracle string in a group description can reach the Reflector and the Editor",
        "grouping_run_secret": "a run secret in a Grouper result is admitted; the journal sees it as [REDACTED]",
        "seal_oracle_leak": "a Candidate that writes an oracle string into the skill is measured and can be promoted",
        "attempt_feedback_screen": "changes holding an oracle string or a run secret reach the next Editor",
        "history_excerpt_screen": "history excerpts holding an oracle string or a run secret reach the Reflector",
        "input_run_secret_redaction": "run secrets in traces reach role inputs and their model providers",
        "connection_description_secret": "an instance description holding a run secret does not stop the start; the journal sees it as [REDACTED]",
        "verbatim_answer_unmatched": "a declared verbatim answer that matches no oracle string is accepted",
        "secret_oracle_path_unmatched": "a declared secret oracle path that selects nothing is accepted",
        "connection_preflight": "the campaign starts without a passed connection preflight",
    }
)


@dataclass(frozen=True)
class CheckModes:
    modes: Mapping[str, Mode]

    def __post_init__(self) -> None:
        object.__setattr__(self, "modes", MappingProxyType(dict(self.modes)))

    def mode(self, name: str) -> Mode:
        return self.modes.get(name, "enforce")

    def non_enforce(self) -> tuple[tuple[str, Mode], ...]:
        return tuple(
            (name, self.mode(name)) for name in CHECK_NAMES if self.mode(name) != "enforce"
        )

    def input_modes(self) -> dict[str, Mode]:
        return {name: self.mode(name) for name in INPUT_CHECKS}

    def to_payload(self) -> dict[str, Any]:
        return {"version": CHECKS_VERSION, "modes": dict(sorted(self.modes.items()))}

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()


ENFORCE_ALL: Final = CheckModes({})


def parse_checks(value: object) -> CheckModes:
    if not isinstance(value, Mapping):
        raise SettingsError("bad_type", "")
    for name in ("version", "modes"):
        if name not in value:
            raise SettingsError("missing_key", name)
    for name in value:
        if name not in ("version", "modes"):
            raise SettingsError("unknown_key", str(name))
    if not isinstance(value["version"], str):
        raise SettingsError("bad_type", "version")
    if value["version"] != CHECKS_VERSION:
        raise SettingsError("bad_value", "version")
    modes = value["modes"]
    if not isinstance(modes, Mapping):
        raise SettingsError("bad_type", "modes")
    parsed: dict[str, Mode] = {}
    for name, mode in modes.items():
        if name not in CHECK_NAMES:
            raise SettingsError("unknown_key", f"modes.{name}")
        if mode not in ("enforce", "shadow", "off"):
            raise SettingsError("bad_value", f"modes.{name}")
        parsed[name] = cast(Mode, mode)
    return CheckModes(parsed)


@dataclass(frozen=True)
class ShadowEntry:
    check: str
    code: str
    count: int
    leak: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.leak is not None:
            object.__setattr__(self, "leak", MappingProxyType(dict(self.leak)))

    def to_payload(self) -> dict[str, Any]:
        value: dict[str, Any] = dict(check=self.check, code=self.code, count=self.count)
        if self.leak is not None:
            value["leak"] = dict(self.leak)
        return value


class HitSink:
    """Invocation-local hits, merged in first-hit order without retaining values."""

    def __init__(self, *, log: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._log = log
        self._entries: dict[bytes, ShadowEntry] = {}
        self._counted: set[tuple[str, str, str]] = set()

    def hit(
        self,
        check: str,
        code: str,
        *,
        leak: Mapping[str, str] | None = None,
        log: Callable[[dict[str, Any]], None] | None = None,
        location: str | None = None,
    ) -> None:
        if location is not None:
            identity = (check, code, location)
            if identity in self._counted:
                return
            self._counted.add(identity)
        entry = ShadowEntry(check, code, 1, leak)
        key = canonical_json(entry.to_payload())
        previous = self._entries.get(key)
        self._entries[key] = ShadowEntry(
            check, code, 1 if previous is None else previous.count + 1, leak
        )
        logger = log if log is not None else self._log
        if logger is not None:
            logger({"event": "check_shadowed", **entry.to_payload()})

    def terminal_fields(self) -> dict[str, Any]:
        """Omit an empty batch, including at callers preserving old signatures."""
        entries = self.entries()
        return {"shadowed": entries} if entries else {}

    def entries(self) -> list[dict[str, Any]]:
        return [entry.to_payload() for entry in self._entries.values()]


_Value = TypeVar("_Value")


def check_predicate(
    check: str,
    code: str,
    predicate: Callable[[_Value], bool],
    *,
    modes: CheckModes = ENFORCE_ALL,
    hits: HitSink | None = None,
    leak: Callable[[], Mapping[str, str] | None] | None = None,
    location: Callable[[], str | None] | None = None,
) -> Callable[[_Value], bool]:
    """Bind a scanner to its mode without changing its private-log redactor."""
    mode = modes.mode(check)

    def forbidden(value: _Value) -> bool:
        if mode == "off":
            return False
        matched = predicate(value)
        if matched and mode == "shadow" and _SILENT.get():
            deferred = _DEFERRED.get()
            if deferred is not None and forbidden not in deferred:
                deferred.append(forbidden)
        if matched and mode == "shadow" and hits is not None and not _SILENT.get():
            hits.hit(
                check,
                code,
                leak=None if leak is None else leak(),
                location=_FIELD.get() if location is None else location(),
            )
        return matched and mode == "enforce"

    cast(Any, forbidden).shadow_scan = lambda value: forbidden(value) if mode == "shadow" else False
    return forbidden


def redact_for_journal(value: Any, secrets: Sequence[str]) -> Any:
    """Redact JSON values without dropping mapping entries whose keys collide."""
    from evolution.tree.execution.input_secrets import redact_run_secrets

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            base = redact_run_secrets(str(key), secrets)
            name, suffix = base, 2
            while name in result:
                name = f"{base}#{suffix}"
                suffix += 1
            result[name] = redact_for_journal(item, secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_for_journal(item, secrets) for item in value]
    return redact_run_secrets(value, secrets)
