"""Typed sample dispositions and stable public reason identifiers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, get_args

Terminal = Literal[
    "completed",
    "turn_budget",
    "time_budget",
    "target_failed",
    "candidate_failed",
    "cancelled",
    "interrupted",
]
SampleOutcome = (
    Terminal
    | Literal[
        "environment_failed",
        "scorer_failed",
        "infrastructure_failed",
    ]
)
SampleDispositionKind = Literal["score", "indeterminate", "exclude"]
SampleOwner = Literal[
    "candidate",
    "target",
    "environment",
    "scorer",
    "rollout",
    "infrastructure",
]
SampleReasonId = Literal[
    "turn_budget_exhausted",
    "rollout_time_budget_exhausted",
    "target_unstable",
    "candidate_turn_failed",
    "environment_unstable",
    "scorer_unstable",
    "rollout_interrupted",
    "rollout_cancelled",
    "infrastructure_failed",
]

_DispositionTuple = tuple[
    SampleDispositionKind,
    SampleOwner | None,
    SampleReasonId | None,
]

_EXPECTED_DISPOSITIONS: dict[SampleOutcome, _DispositionTuple] = {
    "completed": ("score", None, None),
    "turn_budget": ("score", "candidate", "turn_budget_exhausted"),
    "time_budget": (
        "indeterminate",
        "rollout",
        "rollout_time_budget_exhausted",
    ),
    "target_failed": ("indeterminate", "target", "target_unstable"),
    "candidate_failed": ("score", "candidate", "candidate_turn_failed"),
    "environment_failed": (
        "indeterminate",
        "environment",
        "environment_unstable",
    ),
    "scorer_failed": ("indeterminate", "scorer", "scorer_unstable"),
    "interrupted": ("indeterminate", "rollout", "rollout_interrupted"),
    "cancelled": ("exclude", "rollout", "rollout_cancelled"),
    "infrastructure_failed": (
        "exclude",
        "infrastructure",
        "infrastructure_failed",
    ),
}

#: The terminals whose registered disposition kind is `score` -- derived from
#: the registry above rather than spelled out, so a fourth scoring terminal is
#: registered once and every consumer follows. PDR-0013 and ADR-0017 write the
#: baseline admission rule as "the try is scored", never as a list of
#: terminals; this is that set, for the one place that validates a payload
#: rather than filtering rows (`journal_schema`'s `sample_imported`).
SCORED_TERMINALS: frozenset[str] = frozenset(
    outcome for outcome in get_args(Terminal) if _EXPECTED_DISPOSITIONS[outcome][0] == "score"
)


@dataclass(frozen=True)
class SampleDisposition:
    """One valid outcome classification from the finite v2 registry."""

    outcome: SampleOutcome
    kind: SampleDispositionKind
    owner: SampleOwner | None
    reason: SampleReasonId | None

    def __post_init__(self) -> None:
        expected = _EXPECTED_DISPOSITIONS.get(self.outcome)
        actual = (self.kind, self.owner, self.reason)
        if expected != actual:
            raise ValueError("sample disposition must match its registered outcome tuple")


def disposition_for(outcome: SampleOutcome) -> SampleDisposition:
    """Return the only registered disposition for ``outcome``."""

    try:
        kind, owner, reason = _EXPECTED_DISPOSITIONS[outcome]
    except KeyError as exc:
        raise ValueError("sample outcome is invalid") from exc
    return SampleDisposition(outcome, kind, owner, reason)


def classify_sample_outcome(outcome: SampleOutcome) -> SampleDispositionKind:
    """Return the scoring disposition for ``outcome``."""

    return disposition_for(outcome).kind


def _reason_registry() -> dict[SampleReasonId, SampleDisposition]:
    registry: dict[SampleReasonId, SampleDisposition] = {}
    for outcome in _EXPECTED_DISPOSITIONS:
        disposition = disposition_for(outcome)
        if disposition.reason is not None:
            registry[disposition.reason] = disposition
    return dict(sorted(registry.items()))


SAMPLE_REASON_SCHEMA_VERSION = 2
SAMPLE_REASON_REGISTRY_V1: Mapping[SampleReasonId, SampleDisposition] = MappingProxyType(
    _reason_registry()
)


def sample_reason_registry_payload() -> dict[str, object]:
    """Return a deterministic, detached JSON-compatible reason registry."""

    return {
        "schema_version": SAMPLE_REASON_SCHEMA_VERSION,
        "reasons": [
            {
                "id": reason,
                "outcome": disposition.outcome,
                "disposition": disposition.kind,
                "owner": disposition.owner,
            }
            for reason, disposition in SAMPLE_REASON_REGISTRY_V1.items()
        ],
    }
