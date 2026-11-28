"""Agent-neutral inputs and artifact handles for local observations."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ObservationError(ValueError):
    """Stable operation failure; no source payload in the public message."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Source:
    path: Path
    role: str = 'trace'
    format: str = 'auto'


@dataclass(frozen=True)
class RetentionPolicy:
    policy_id: str = 'structure-only'
    policy_version: str = '1'
    attribute_keys: tuple[str, ...] = ()
    metadata_keys: tuple[str, ...] = ()


@dataclass
class ImportGroup:
    sources: list[Source]
    namespace: str | None = None
    namespace_origin: str = 'declared'
    namespace_source_ref: str | None = None
    mapper: Any = None
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    declared_facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceEnvelope:
    trace_id: str
    spans: list[dict[str, Any]]
    producer: dict[str, Any]
    facts: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]


@dataclass
class MappedExecution:
    invocation_key: str | None = None
    identity_basis: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    input: Any = None
    context: Any = None
    output: Any = None
    status: Any = None
    steps: list[dict[str, Any]] | None = None
    version_facts: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MappingResult:
    executions: list[MappedExecution] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Selection:
    execution_ref: str
    replacements: dict[str, Any] = field(default_factory=dict)
    group_ref: str | None = None
    criteria_ref: str | None = None
    world_ref: str | None = None


# Public diagnostics use a closed vocabulary; mapper strings remain private.
PUBLIC_CODES = frozenset({
    'source_unreadable', 'unsupported_format', 'source_decode_failed',
    'mapping_failed', 'mapping_empty', 'mapper_diagnostic', 'span_invalid',
    'span_conflict', 'representation_uncertain', 'span_incomplete',
    'payload_reduced', 'possible_trace_fragmentation', 'identity_unknown',
    'execution_unavailable', 'input_modified', 'unknown',
})


@dataclass
class Artifact:
    path: Path
    manifest: dict[str, Any]
    records: dict[str, list[dict[str, Any]]]

    def public(self) -> dict[str, Any]:
        diagnostics = list(self.manifest.get('diagnostics', []))
        # Manifest is the diagnostic registry; row copies are contextual links.
        # Counting them again would multiply events by the number of executions.
        counts = Counter(d.get('code') if d.get('code') in PUBLIC_CODES else 'unknown'
                         for d in diagnostics)
        return {'revision': self.manifest['revision'], 'schema': self.manifest['schema'],
                'counts': {k.removesuffix('.jsonl'): len(v) for k, v in self.records.items()},
                'diagnostic_counts': dict(counts), 'partial': self.manifest.get('partial', False)}


class ObservationSet(Artifact):
    @property
    def spans(self): return self.records['spans.jsonl']

    @property
    def executions(self): return self.records['executions.jsonl']

    @classmethod
    def open(cls, path: Path):
        from .storage import read_artifact
        return read_artifact(path, 'lab-observations/1', cls)


class Basket(Artifact):
    @property
    def cases(self): return self.records['cases.jsonl']

    @classmethod
    def open(cls, path: Path):
        from .storage import read_artifact
        return read_artifact(path, 'lab-basket/1', cls)


@dataclass
class RecordedEvaluationPlan:
    path: Path
    data: dict[str, Any]

    def public(self):
        return {'schema': self.data['schema'], 'revision': self.data['revision'],
                'case_count': len(self.data['cases']),
                'available_records': sum(row['record_available'] for row in self.data['cases'])}
