"""Manifest /16 evidence settings; round input limits have no upper bound."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, cast

EVIDENCE_VERSION: Final = "evidence/1"
EVIDENCE_DEFAULTS: Final[Mapping[str, int | str]] = MappingProxyType(
    {
        "view": "full",
        "turns": 8,
        "events_per_turn": 32,
        "text_chars": 4096,
        "input_chars": 2048,
        "feedback_keys": 16,
        "feedback_value_chars": 64,
        "cited_chars": 65536,
        "trace_page_chars": 16000,
    }
)
EVIDENCE_MINIMUMS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "turns": 1,
        "events_per_turn": 2,
        "text_chars": 32,
        "input_chars": 0,
        "feedback_keys": 0,
        "feedback_value_chars": 1,
        "cited_chars": 0,
        "trace_page_chars": 256,
    }
)
_OBSERVATION_LIMITS: Final = (
    "turns",
    "events_per_turn",
    "text_chars",
    "input_chars",
    "feedback_keys",
    "feedback_value_chars",
)


class SettingsError(ValueError):
    """A closed refusal code and relative path, never a supplied value."""

    def __init__(self, code: str, path: str) -> None:
        super().__init__(f"{code}: {path}")
        self.code = code
        self.path = path


@dataclass(frozen=True)
class EvidenceSettings:
    view: Literal["full", "allowlist"]
    turns: int
    events_per_turn: int
    text_chars: int
    input_chars: int
    feedback_keys: int
    feedback_value_chars: int
    cited_chars: int
    trace_page_chars: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": EVIDENCE_VERSION,
            **{name: getattr(self, name) for name in EVIDENCE_DEFAULTS},
        }

    @classmethod
    def from_payload(cls, payload: object) -> EvidenceSettings:
        return parse_evidence(payload)

    def limits(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _OBSERVATION_LIMITS}


def parse_evidence(value: object) -> EvidenceSettings:
    if not isinstance(value, Mapping):
        raise SettingsError("bad_type", "")
    if "version" not in value:
        raise SettingsError("missing_key", "version")
    for name in value:
        if name != "version" and name not in EVIDENCE_DEFAULTS:
            raise SettingsError("unknown_key", str(name))
    if not isinstance(value["version"], str):
        raise SettingsError("bad_type", "version")
    if value["version"] != EVIDENCE_VERSION:
        raise SettingsError("bad_value", "version")
    view = value.get("view", EVIDENCE_DEFAULTS["view"])
    if not isinstance(view, str):
        raise SettingsError("bad_type", "view")
    if view not in ("full", "allowlist"):
        raise SettingsError("bad_value", "view")
    limits: dict[str, int] = {}
    for name, minimum in EVIDENCE_MINIMUMS.items():
        number = value.get(name, EVIDENCE_DEFAULTS[name])
        if type(number) is not int:
            raise SettingsError("bad_type", name)
        if number < minimum:
            raise SettingsError("bad_value", name)
        limits[name] = number
    return EvidenceSettings(view=cast(Literal["full", "allowlist"], view), **limits)


DEFAULT_EVIDENCE: Final = parse_evidence({"version": EVIDENCE_VERSION})


@dataclass(frozen=True)
class EvidencePin:
    """The immutable evidence settings and input modes a round opened with."""

    settings: EvidenceSettings
    input_modes: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_modes", MappingProxyType(dict(self.input_modes)))

    def payload(self) -> dict[str, Any]:
        return {"evidence": self.settings.to_payload(), "input_checks": dict(self.input_modes)}
