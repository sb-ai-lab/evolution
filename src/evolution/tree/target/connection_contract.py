"""Manifest `/15`'s connection declarations: `metric_contract/1`, `role_visibility/1` and
the optional `verbatim_answers/1`.

ADR-0029 owns metrics and visibility. ADR-0038 owns verbatim answers. All three objects are closed: an
unknown key, a missing key or a wrong type is refused before any external call. A refusal is a `ContractError(code, path)`
with `code` one of `unknown_key | missing_key | bad_type | bad_value` and
`path` relative to the object (`""` for the object itself); the manifest
loader prefixes it and attaches the fixed sentence
(`manifest_v15_help.connection_expected`, or `VERBATIM_ANSWERS_EXPECTED` for
`verbatim_answers/1`). No refusal carries a value.

`metric_contract/1` is bounded at admission: at most 64 `target_fields`, an
`event` of 1 to 128 UTF-8 bytes that is not blank, a `pointer` of at most
1,024 UTF-8 bytes that is a valid RFC 6901 JSON Pointer, a `value` from seven
names, a canonical object of at most 64 KiB, and no `(event, pointer)` pair
twice (refused at the later entry). `/15` admits only `kind: pointwise`;
`collection` is refused until a later schema.

`role_visibility/1` lists artifact path patterns in the artifact grammar
(`artifact_policy.validate_pattern`), in the written order, each once. An
empty `target_fields` or `readable_paths` list is valid.

`verbatim_answers/1` is optional and lists 1 to 64 distinct `strings`, each of
1 to 4,096 UTF-8 bytes; newlines are allowed. An empty `strings` list is
refused: omitting the key declares none. Every shape refusal carries one fixed
sentence, `manifest_v15_help.VERBATIM_ANSWERS_EXPECTED`.

The canonical payloads' digests are what the campaign identity binds
(`ConnectionContract.identity_fields`): `metric_contract` is a comparable
field owned by the Scorer's condition window (M8), `role_visibility` is not
comparable, and `verbatim_answers` is bound, not comparable, only when it is
declared, so an undeclared contract keeps the b1/b2 identity bytes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from evolution.tree.artifacts.artifact_policy import validate_pattern
from evolution.tree.execution.check_modes import CheckModes
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.json_pointer import _pointer_tokens

METRIC_CONTRACT_VERSION: Final = "metric_contract/1"
ROLE_VISIBILITY_VERSION: Final = "role_visibility/1"
VERBATIM_ANSWERS_VERSION: Final = "verbatim_answers/1"
SECRET_ORACLE_PATHS_VERSION: Final = "secret_oracle_paths/1"
MAX_SECRET_ORACLE_PATHS: Final = 64
_SECRET_PATHS_KEYS: Final = frozenset({"version", "pointers"})
MAX_VERBATIM_ANSWERS: Final = 64
MAX_VERBATIM_ANSWER_BYTES: Final = 4096
_VERBATIM_KEYS: Final = frozenset({"version", "strings"})
ValueRule = Literal[
    "nonempty_string", "string", "finite_number", "boolean", "object", "array", "finite_json"
]
VALUE_RULES: Final[tuple[ValueRule, ...]] = (
    "nonempty_string",
    "string",
    "finite_number",
    "boolean",
    "object",
    "array",
    "finite_json",
)
MAX_TARGET_FIELDS: Final = 64
MAX_EVENT_BYTES: Final = 128
MAX_POINTER_BYTES: Final = 1024
MAX_CONTRACT_BYTES: Final = 64 * 1024

_CONTRACT_KEYS = frozenset({"version", "kind", "target_fields"})
_FIELD_KEYS = frozenset({"event", "pointer", "value"})
_VISIBILITY_KEYS = frozenset({"version", "readable_paths"})


class ContractError(ValueError):
    """A closed-code refusal of a connection declaration, at a relative path.

    `section` is set for `unknown_key` only: the relative path of the object
    that holds the unknown key (`""` for the declaration itself), which picks
    the sentence, as the campaign policy's structure pass does.
    """

    def __init__(self, code: str, path: str, *, section: str | None = None) -> None:
        super().__init__(f"{code}: {path}")
        self.code = code
        self.path = path
        self.section = section


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _closed(value: Mapping[str, Any], keys: frozenset[str], at: str) -> None:
    missing = sorted(keys - value.keys())
    if missing:
        raise ContractError("missing_key", _join(at, missing[0]))
    unknown = sorted(value.keys() - keys)
    if unknown:
        raise ContractError("unknown_key", _join(at, unknown[0]), section=at)


def _utf8_length(value: str, path: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ContractError("bad_value", path) from None


@dataclass(frozen=True)
class TargetField:
    event: str
    pointer: str
    value: ValueRule


@dataclass(frozen=True)
class MetricContract:
    kind: Literal["pointwise"]
    target_fields: tuple[TargetField, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": METRIC_CONTRACT_VERSION,
            "kind": self.kind,
            "target_fields": [
                {"event": field.event, "pointer": field.pointer, "value": field.value}
                for field in self.target_fields
            ],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()


@dataclass(frozen=True)
class RoleVisibility:
    readable_paths: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"version": ROLE_VISIBILITY_VERSION, "readable_paths": list(self.readable_paths)}

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()


@dataclass(frozen=True)
class VerbatimAnswers:
    """`/15`'s optional `verbatim_answers/1` (ADR-0038): canned answers the agent
    must give verbatim, which the operator declares are not oracle material."""

    strings: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"version": VERBATIM_ANSWERS_VERSION, "strings": list(self.strings)}

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()


@dataclass(frozen=True)
class SecretOraclePaths:
    """The operator's selected oracle subtrees, in declared order (/16)."""

    pointers: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"version": SECRET_ORACLE_PATHS_VERSION, "pointers": list(self.pointers)}

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()


@dataclass(frozen=True)
class ConnectionContract:
    """The `/15` declarations together, as the campaign identity binds them."""

    metric_contract: MetricContract
    role_visibility: RoleVisibility
    #: Declared optionally (ADR-0038); bound by `identity_fields` only when declared.
    verbatim_answers: VerbatimAnswers | None = None
    secret_oracle_paths: SecretOraclePaths | None = None
    checks: CheckModes | None = None
    preflight_sample_retries: int | None = None

    def identity_fields(self) -> dict[str, str | int]:
        """The two connection digests, plus `verbatim_answers` exactly when declared
        (ADR-0038), so an undeclared contract keeps the b1/b2 identity bytes."""

        fields: dict[str, str | int] = {
            "metric_contract": self.metric_contract.digest,
            "role_visibility": self.role_visibility.digest,
        }
        if self.verbatim_answers is not None:
            fields["verbatim_answers"] = self.verbatim_answers.digest
        if self.secret_oracle_paths is not None:
            fields["secret_oracle_paths"] = self.secret_oracle_paths.digest
        if self.checks is not None:
            fields["checks"] = self.checks.digest
        if self.preflight_sample_retries is not None:
            fields["preflight_sample_retries"] = self.preflight_sample_retries
        return fields


def _version(value: Mapping[str, Any], expected: str) -> None:
    version = value["version"]
    if not isinstance(version, str):
        raise ContractError("bad_type", "version")
    if version != expected:
        raise ContractError("bad_value", "version")


def _event(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError("bad_type", path)
    if not value.strip() or _utf8_length(value, path) > MAX_EVENT_BYTES:
        raise ContractError("bad_value", path)
    return value


def _pointer(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError("bad_type", path)
    if _utf8_length(value, path) > MAX_POINTER_BYTES:
        raise ContractError("bad_value", path)
    try:
        _pointer_tokens(value)
    except ValueError:
        raise ContractError("bad_value", path) from None
    return value


def _value_rule(value: object, path: str) -> ValueRule:
    if not isinstance(value, str):
        raise ContractError("bad_type", path)
    for rule in VALUE_RULES:
        if value == rule:
            return rule
    raise ContractError("bad_value", path)


def parse_metric_contract(value: object) -> MetricContract:
    """The closed `metric_contract/1` object, or the first `ContractError`.

    Order: the object's own keys, `version`, `kind`, `target_fields` and its
    length, then each entry in order (its keys, `event`, `pointer`, `value`,
    the duplicate check), then the canonical size.
    """

    if not isinstance(value, dict):
        raise ContractError("bad_type", "")
    _closed(value, _CONTRACT_KEYS, "")
    _version(value, METRIC_CONTRACT_VERSION)
    kind = value["kind"]
    if not isinstance(kind, str):
        raise ContractError("bad_type", "kind")
    if kind != "pointwise":
        raise ContractError("bad_value", "kind")
    raw = value["target_fields"]
    if not isinstance(raw, list):
        raise ContractError("bad_type", "target_fields")
    if len(raw) > MAX_TARGET_FIELDS:
        raise ContractError("bad_value", "target_fields")
    fields: list[TargetField] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        at = f"target_fields[{index}]"
        if not isinstance(item, dict):
            raise ContractError("bad_type", at)
        _closed(item, _FIELD_KEYS, at)
        event = _event(item["event"], f"{at}.event")
        pointer = _pointer(item["pointer"], f"{at}.pointer")
        rule = _value_rule(item["value"], f"{at}.value")
        if (event, pointer) in seen:
            raise ContractError("bad_value", at)
        seen.add((event, pointer))
        fields.append(TargetField(event, pointer, rule))
    parsed = MetricContract("pointwise", tuple(fields))
    if len(canonical_json(parsed.to_payload())) > MAX_CONTRACT_BYTES:
        raise ContractError("bad_value", "")
    return parsed


def parse_verbatim_answers(value: object) -> VerbatimAnswers:
    """The closed `verbatim_answers/1` object, or the first `ContractError` (never a value)."""

    if not isinstance(value, dict):
        raise ContractError("bad_type", "")
    _closed(value, _VERBATIM_KEYS, "")
    _version(value, VERBATIM_ANSWERS_VERSION)
    raw = value["strings"]
    if not isinstance(raw, list):
        raise ContractError("bad_type", "strings")
    if not 1 <= len(raw) <= MAX_VERBATIM_ANSWERS:
        raise ContractError("bad_value", "strings")
    strings: list[str] = []
    for index, item in enumerate(raw):
        at = f"strings[{index}]"
        if not isinstance(item, str):
            raise ContractError("bad_type", at)
        if not 1 <= _utf8_length(item, at) <= MAX_VERBATIM_ANSWER_BYTES:
            raise ContractError("bad_value", at)
        if item in strings:
            raise ContractError("bad_value", at)
        strings.append(item)
    return VerbatimAnswers(tuple(strings))


def parse_role_visibility(value: object) -> RoleVisibility:
    """The closed `role_visibility/1` object, or the first `ContractError`."""

    if not isinstance(value, dict):
        raise ContractError("bad_type", "")
    _closed(value, _VISIBILITY_KEYS, "")
    _version(value, ROLE_VISIBILITY_VERSION)
    raw = value["readable_paths"]
    if not isinstance(raw, list):
        raise ContractError("bad_type", "readable_paths")
    paths: list[str] = []
    for index, item in enumerate(raw):
        at = f"readable_paths[{index}]"
        if not isinstance(item, str):
            raise ContractError("bad_type", at)
        _utf8_length(item, at)
        try:
            validate_pattern(item)
        except ValueError:
            raise ContractError("bad_value", at) from None
        if item in paths:
            raise ContractError("bad_value", at)
        paths.append(item)
    return RoleVisibility(tuple(paths))


def parse_secret_oracle_paths(value: object) -> SecretOraclePaths:
    """Parse a closed /16 declaration; refusals name a position, never a value."""

    if not isinstance(value, dict):
        raise ContractError("bad_type", "")
    _closed(value, _SECRET_PATHS_KEYS, "")
    _version(value, SECRET_ORACLE_PATHS_VERSION)
    raw = value["pointers"]
    if not isinstance(raw, list):
        raise ContractError("bad_type", "pointers")
    if len(raw) > MAX_SECRET_ORACLE_PATHS:
        raise ContractError("bad_value", "pointers")
    pointers: list[str] = []
    for index, item in enumerate(raw):
        at = f"pointers[{index}]"
        pointer = _pointer(item, at)
        if pointer in pointers:
            raise ContractError("bad_value", at)
        pointers.append(pointer)
    return SecretOraclePaths(tuple(pointers))
