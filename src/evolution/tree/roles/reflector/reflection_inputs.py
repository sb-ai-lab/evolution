"""Lazy, baseline-bound inputs for family-4 grouping and reflection.

This is deliberately a campaign-domain object rather than a role helper.  It
keeps the measurement prefix, verified candidate view, and source locators in
process, while its handlers expose only bounded safe projections.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Final

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
from evolution.tree.artifacts.artifacts import ArtifactRef
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.execution.check_modes import HitSink
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.interaction import CandidateView, EvalCase
from evolution.tree.execution.json_pointer import _pointer_tokens, pointer_value
from evolution.tree.execution.rollout_scoring import _parse_score_result, parse_scorer_stdout
from evolution.tree.execution.scorer_traces import decode_trace_text
from evolution.tree.roles.editor.editor_brief import _observation_evidence_id
from evolution.tree.roles.editor.editor_evidence import (
    CarriedTraces,
    _bound_view,
    _scoring_records,
    _verified_view,
    read_trace,
)
from evolution.tree.roles.editor_contracts import plain_public, reflector_observation_payload
from evolution.tree.roles.environment_docs import (
    _v2_catalog_page,
    _v2_read_document_page,
    _V2DocumentSnapshot,
)
from evolution.tree.roles.grouping.reflect_grouping import (
    GroupingRecord,
    GroupingRegistry,
    PortionCohort,
    build_registry,
)
from evolution.tree.roles.input_secrets import criteria_carry_run_secret
from evolution.tree.roles.reflection_reader import (
    BODY_CACHE_BYTES,
    CONTENT_CHARS,
    GROUPING_PAGE_SIZE,
    MAX_RESPONSE_BYTES,
    ByteLruCache,
    FrozenReflectionView,
    ReaderArgumentError,
    ReaderIntegrityError,
)
from evolution.tree.roles.reflector.reflect_evidence import RepeatEvidence, _repeat_evidence
from evolution.tree.roles.reflector_contracts import REFLECT_LIMITS_V1
from evolution.tree.storage.envelope import Envelope

EVIDENCE_PAGE_SIZE: Final = 32
SUMMARY_CASE_ID_BYTES: Final = 512
SUMMARY_VARIANTS: Final = 8

__all__ = ["FrozenReflectionInputs", "CONTENT_CHARS", "EVIDENCE_PAGE_SIZE", "GROUPING_PAGE_SIZE"]


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ReaderIntegrityError("invalid_admitted_projection") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _page(text: str, offset: object) -> dict[str, object]:
    if type(offset) is not int or offset < 0 or offset > len(text):
        raise ReaderArgumentError("invalid_offset", f"offset must be between 0 and {len(text)}")
    end = min(len(text), offset + CONTENT_CHARS)
    return {
        "content": text[offset:end],
        "content_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "offset": offset,
        "total_characters": len(text),
        "next_offset": end if end < len(text) else None,
    }


def _offset(arguments: Mapping[str, Any], *, default: int = 0, total: int | None = None) -> int:
    value = arguments.get("offset", default)
    if type(value) is not int or value < 0:
        raise ReaderArgumentError(
            "invalid_offset",
            f"offset must be between 0 and {total}"
            if total is not None
            else "offset must be a non-negative integer",
        )
    return value


def _bounded_entries(
    entries: Sequence[object], offset: int, count: int
) -> tuple[list[object], int]:
    """Take a page whose enclosing catalog response stays under 128 KiB."""

    page: list[object] = []
    end = offset
    for entry in entries[offset : offset + count]:
        candidate = page + [entry]
        # Leave room for the stable page fields, continuation, and JSON syntax.
        if (
            len(
                _canonical(
                    {"entries": candidate, "total": len(entries), "next_offset": end + 1}
                ).encode()
            )
            > MAX_RESPONSE_BYTES
        ):
            if not page:
                raise ReaderIntegrityError("catalog_entry_too_large")
            break
        page.append(entry)
        end += 1
    return page, end


@dataclass(frozen=True)
class _Header:
    row: SampleRow
    case: EvalCase | None
    evidence_id: str
    available: bool
    failing: bool
    terminal: str
    critical: bool | None
    failed_criteria: tuple[str, ...] | None
    #: `(location, section, sha256)` of the trace sections the header was
    #: authenticated from; memory only (ADR-0040 D6). `None` stands for a
    #: section that could not be read.
    fingerprints: tuple[tuple[str | None, str, str | None], ...]


class _Sources(Mapping[str, object]):
    """The grouping projections, fetched only by a selected feature.

    Four projections always; a fifth, ``"diagnostics"``, only when the owner's
    ``_scorer_diagnostics`` admits it (family 8, ADR-0021) -- this is the
    runtime side of `reflect_grouping.GroupingRecord`'s own admitted set.
    """

    def __init__(self, owner: FrozenReflectionInputs, index: int) -> None:
        self.owner = owner
        self.index = index

    def _keys(self) -> tuple[str, ...]:
        base = ("case", "public", "observation", "feedback")
        return base + ("diagnostics",) if self.owner._scorer_diagnostics else base

    def __getitem__(self, key: str) -> object:
        if key not in self._keys():
            raise KeyError(key)
        if key == "observation":
            return LazyObservationMapping(self.owner, self.index)
        return self.owner._section(self.index, key)

    def __iter__(self):
        return iter(self._keys())

    def __len__(self) -> int:
        return len(self._keys())


class LazyObservationMapping(Mapping[str, object]):
    """Expose authenticated observation headers without retaining trace bodies.

    The three fields used by the built-in grouping and representative signature
    come from the authenticated construction header.  Looking up any other
    field, or iterating the mapping for a root-pointer feature, realizes the
    complete bounded projection only at that point.
    """

    _HEADER_FIELDS: Final = frozenset({"terminal", "critical", "failed_criteria"})

    def __init__(self, owner: FrozenReflectionInputs, index: int) -> None:
        self._owner = owner
        self._index = index

    def __getitem__(self, key: str) -> object:
        if key in self._HEADER_FIELDS:
            return self._owner._observation_header(self._index)[key]
        return self._body()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._body())

    def __len__(self) -> int:
        return len(self._body())

    def _body(self) -> Mapping[str, object]:
        value = self._owner._section(self._index, "observation")
        assert isinstance(value, Mapping)
        return value


class FrozenReflectionInputs:
    """One frozen baseline's registry and lazy evidence/catalog handlers.

    Construction authenticates each started record only long enough to retain
    its mechanical header; it does not retain ``RepeatEvidence`` bodies.  A
    later read rechecks the bound trace fingerprints before using a small,
    disposable byte LRU for the projected section bytes.
    """

    def __init__(
        self,
        *,
        baseline_binding: str,
        rows: Sequence[SampleRow],
        records: Sequence[Envelope],
        run_root: Path,
        corpus: Sequence[EvalCase],
        baseline: ArtifactRef,
        environment_kind: str,
        metric_primary: str,
        reflection_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        families: Mapping[str, str | None] | None = None,
        families_configured: bool = False,
        limits: Mapping[str, int] = REFLECT_LIMITS_V1,
        context_page_size: int = EVIDENCE_PAGE_SIZE,
        documents: _V2DocumentSnapshot | None = None,
        body_cache: ByteLruCache | None = None,
        integrity_check: Callable[[], None] | None = None,
        admission_check: Callable[[], None] | None = None,
        scorer_diagnostics: bool = False,
        model_grouping_family: bool = False,
        carried: CarriedTraces | None = None,
        role_visible: Callable[[str], bool] | None = None,
        secrets: Sequence[str] = (),
        pin: EvidencePin | None = None,
        hits: HitSink | None = None,
        family15: bool = False,
    ) -> None:
        if not isinstance(baseline_binding, str) or not baseline_binding:
            raise ValueError("baseline_binding")
        self.baseline_binding = baseline_binding
        self.family15 = family15
        #: Family 8 only (ADR-0021): whether the bound campaign's scorer output
        #: grammar admits `diagnostics`, which gates the `"diagnostics"`
        #: section/source everywhere below. False keeps families 5-7 byte
        #: for byte -- the section stays unknown and the source stays absent.
        self._scorer_diagnostics = bool(scorer_diagnostics)
        self._secrets = tuple(secrets)
        self.pin = pin
        self._hits = hits
        #: Family 9 only (ADR-0022): whether the bound campaign's adaptive
        #: Reflector brief is `reflect_brief/4`, whose selected groups carry
        #: their admitted `description`. False keeps families 5-8 byte for byte.
        self._model_grouping_family = bool(model_grouping_family)
        self._run_root = Path(run_root)
        #: One reader for the whole frozen input: it caches pack indexes.
        from evolution.tree.baseline_diagnostics import DiagnosticTraceReader

        self._traces = DiagnosticTraceReader(self._run_root)
        from evolution.tree.baseline_traces import BaselineTraces

        self.traces = (
            None
            if pin is None
            else BaselineTraces(
                traces=self._traces,
                measurement_records=records,
                pin=pin,
                secrets=secrets,
                corpus=corpus,
                baseline=baseline,
                carried=carried,
                hits=hits,
            )
        )
        self._limits = limits
        if type(context_page_size) is not int or not 1 <= context_page_size <= 64:
            raise ValueError("context_page_size")
        self.context_page_size = context_page_size
        #: Family 12 only (ADR-0017, amended 2026-10-02): a row carried from a
        #: measurement of the same run is read through its source try's
        #: scoring records; `None` keeps every other family byte for byte.
        self._identity, self._started, self._finished = _scoring_records(records, carried)
        #: Family 14 only (PDR-0023, ADR-0029): the Reflector's path predicate.
        #: `read_evidence` renders the observation section through it; the
        #: grouping readers and `_Sources` never do (ADR-0034: the Grouper keeps
        #: reading unfiltered observations). `None` keeps families up to 13
        #: byte for byte.
        self._role_visible = role_visible
        scorer = self._identity.get("scorer", {})
        self._scorer: Mapping[str, Any] = scorer if isinstance(scorer, Mapping) else {}
        self._candidate: CandidateView | None = _verified_view(baseline)  # exactly once
        self._environment_kind = environment_kind
        self._metric_primary = metric_primary
        self._cases = {case.case_id: case for case in corpus}
        self._metadata = {
            case_id: plain_public(value) for case_id, value in (reflection_metadata or {}).items()
        }
        self._families = {
            case_id: family for case_id, family in (families or {}).items() if family is not None
        }
        #: Whether the campaign's families policy is `metadata`, kept apart from
        #: `_families`, which drops every absent value: a configured policy
        #: under which no case has a family is still a configured one.
        #:
        self._families_configured = bool(families_configured)
        self._cache = body_cache or ByteLruCache(BODY_CACHE_BYTES)
        self._documents = documents
        self._extra_integrity_check = integrity_check
        self._extra_admission_check = admission_check
        self._headers: list[_Header] = []
        published: set[str] = set()
        for row in rows:
            if row.disposition == "not_started":
                continue
            case = self._cases.get(row.case_id)
            available, failing, failed_criteria, criteria = self._header_flags(row, case)
            fingerprints = self._trace_fingerprints(row)
            if available and fingerprints:
                published.update(criteria)
            self._headers.append(
                _Header(
                    row,
                    case,
                    _observation_evidence_id(row.case_id, row.repeat),
                    available and bool(fingerprints),
                    failing,
                    row.outcome or "not_started",
                    row.critical_pass,
                    failed_criteria,
                    fingerprints,
                )
            )
        self._headers.sort(key=lambda header: (header.row.case_id, header.row.repeat))
        #: Every verdict `criterion`, passed or failed, the scorer published on
        #: an authenticated observation of this baseline. Never
        #: a verdict note, a `diagnostics` value or a criterion of a row whose
        #: scorer output did not authenticate. Family 14's reflection-side
        #: needle set drops oracle strings exactly equal to one of them
        #: (`campaign_reflector.reflection_needles`); earlier families never
        #: read it.
        self.published_criteria: frozenset[str] = frozenset(published)
        provisional = [
            GroupingRecord(
                header.evidence_id,
                header.row.case_id,
                header.row.repeat,
                header.failing,
                header.available,
                _Sources(self, index),
                role_observation_value=partial(self._role_observation_value, index)
                if self.family15 and self._role_visible is not None
                else None,
            )
            for index, header in enumerate(self._headers)
        ]
        self.registry: GroupingRegistry = build_registry(baseline_binding, provisional)
        self._evidence_index = {
            header.evidence_id: index for index, header in enumerate(self._headers)
        }
        self._case_ids = tuple(sorted({header.row.case_id for header in self._headers}))
        self._case_positions = {case_id: index for index, case_id in enumerate(self._case_ids)}
        indices: dict[str, list[int]] = {case_id: [] for case_id in self._case_ids}
        for index, header in enumerate(self._headers):
            indices[header.row.case_id].append(index)
        self._case_indices = {case_id: tuple(value) for case_id, value in indices.items()}
        self._case_counts = {
            case_id: (
                sum(self._headers[index].available for index in values),
                sum(self._headers[index].failing for index in values),
                sum(not self._headers[index].available for index in values),
            )
            for case_id, values in self._case_indices.items()
        }
        grouped_families: dict[str, list[str]] = {}
        for case_id in self._case_ids:
            family = self._families.get(case_id)
            if family is not None:
                grouped_families.setdefault(family, []).append(case_id)
        self._family_case_ids = {
            family: tuple(case_ids) for family, case_ids in grouped_families.items()
        }
        self._case_handles = {
            case_id: "case_"
            + hashlib.sha256((baseline_binding + "\0" + case_id).encode()).hexdigest()[:24]
            for case_id in self._case_ids
        }
        self._handle_cases = {value: key for key, value in self._case_handles.items()}

    @property
    def families_configured(self) -> bool:
        """Whether the campaign's families policy is `metadata`, whatever values
        the corpus carries."""

        return self._families_configured

    @property
    def scorer_diagnostics(self) -> bool:
        """Family 8 only (ADR-0021): whether this baseline's scorer output
        grammar admits `diagnostics`. The one source of truth for every
        family-8-gated surface this object serves -- the `"diagnostics"`
        section/source here, and the sealed custom grouping request version a
        caller (`_Campaign.run_grouping`) builds from it."""

        return self._scorer_diagnostics

    @property
    def model_grouping_family(self) -> bool:
        """Family 9 only (ADR-0022): whether the adaptive Reflector brief built
        over these inputs is `reflect_brief/4` -- `scorer_diagnostics`'s
        family-9 twin, and the one gate `build_adaptive_reflect_brief` reads."""

        return self._model_grouping_family

    def diagnostics_for(self, evidence_id: str) -> object:
        """Family 8 only (ADR-0021): one evidence id's raw `diagnostics`
        value, or `None` when absent or unavailable -- `reflect_brief`'s
        `exemplar_diagnostics` reads this rather than `_section` directly."""

        return self._section(self._evidence_index[evidence_id], "diagnostics")

    def handlers(self) -> Mapping[str, Callable[[Mapping[str, Any]], Any]]:
        handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "list_grouping_inputs": self.list_grouping_inputs,
            "read_grouping_input": self.read_grouping_input,
            "stream_grouping_inputs": self.stream_grouping_inputs,
            "list_evidence": self.list_evidence,
            "read_evidence": self.read_evidence,
        }
        if self.traces is not None:
            handlers["read_trace"] = self.traces.read
        if self._documents is not None:
            handlers.update(
                {"list_documents": self.list_documents, "read_document": self.read_document}
            )
        return handlers

    @property
    def body_cache(self) -> ByteLruCache:
        """The baseline's bounded body cache: a reflection's history
        handlers cache their bodies here under reflection-scoped keys."""

        return self._cache

    def reader_view(
        self, *, extra_handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]] | None = None
    ) -> FrozenReflectionView:
        """`extra_handlers`: one reflection's `list_history` and
        `read_history`, which are never part of this baseline-cached object."""

        handlers = dict(self.handlers())
        if extra_handlers:
            if handlers.keys() & extra_handlers.keys():
                raise ValueError("reader_handlers")
            handlers.update(extra_handlers)
        return FrozenReflectionView(
            handlers, integrity_check=self.verify_integrity, body_cache=self._cache
        )

    def verify_integrity(self) -> None:
        if self._extra_integrity_check is not None:
            self._extra_integrity_check()

    def verify_admission(self) -> None:
        """Perform the one final source scan required before role admission.

        Call this once after a role finishes reading and before accepting its
        output. Reader dispatch checks the campaign control plane; this method
        closes the interval after the role's final read without making every
        page traversal rescan the complete baseline.
        """

        self.verify_integrity()
        if self._extra_admission_check is not None:
            self._extra_admission_check()
        for header in self._headers:
            self._verify_header(header)

    def _section_names(self) -> tuple[str, ...]:
        """The evidence/grouping-input section names this baseline serves.

        Family 8 only (ADR-0021) adds ``"diagnostics"``; families 5-7 keep the
        five original names byte for byte.
        """

        base = ("identity", "metadata", "public", "observation", "feedback")
        return base + ("diagnostics",) if self._scorer_diagnostics else base

    def list_grouping_inputs(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        self._check_arguments(arguments, {"offset"})
        offset = _offset(arguments)
        if offset > len(self._headers):
            raise ReaderArgumentError("invalid_offset")
        entries: list[object] = []
        for index, header in enumerate(
            self._headers[offset : offset + GROUPING_PAGE_SIZE], start=offset
        ):
            entries.append(
                {
                    "index": index,
                    "evidence_id": header.evidence_id,
                    "case_index": self._case_positions[header.row.case_id],
                    "repeat": header.row.repeat,
                    "available": header.available,
                    "failing": header.failing,
                }
            )
        page, next_offset = _bounded_entries(entries, 0, GROUPING_PAGE_SIZE)
        absolute_end = offset + next_offset
        return {
            "entries": page,
            "total": len(self._headers),
            "next_offset": absolute_end if absolute_end < len(self._headers) else None,
        }

    def read_grouping_input(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        self._check_arguments(
            arguments, {"index", "section", "offset"}, required={"index", "section"}
        )
        index = arguments["index"]
        section = arguments["section"]
        if type(index) is not int or not 0 <= index < len(self._headers):
            raise ReaderArgumentError("unknown_registry_index")
        if section not in self._section_names():
            raise ReaderArgumentError("unknown_section")
        response = _page(_canonical(self._section(index, section)), arguments.get("offset", 0))
        if section == "feedback":
            response.update(self._feedback_markers(index))
        return response

    def stream_grouping_inputs(self, arguments: Mapping[str, Any]) -> Iterator[dict[str, object]]:
        self._check_arguments(arguments, {"start", "stop", "sections"})
        start = arguments.get("start", 0)
        stop = arguments.get("stop", len(self._headers))
        sections = arguments.get("sections")
        if (
            type(start) is not int
            or type(stop) is not int
            or not 0 <= start <= stop <= len(self._headers)
        ):
            raise ReaderArgumentError("invalid_range")
        if not isinstance(sections, list) or not sections or len(set(sections)) != len(sections):
            raise ReaderArgumentError("invalid_sections")
        if any(section not in self._section_names() for section in sections):
            raise ReaderArgumentError("unknown_section")
        yield {
            "kind": "header",
            "registry_digest": self.registry.digest,
            "record_count": len(self._headers),
            "start": start,
            "stop": stop,
        }
        for index in range(start, stop):
            header = self._headers[index]
            for section in sections:
                text = _canonical(self._section(index, section))
                for offset in range(0, max(len(text), 1), CONTENT_CHARS):
                    chunk = text[offset : offset + CONTENT_CHARS]
                    frame: dict[str, object] = {
                        "kind": "data",
                        "index": index,
                        "evidence_id": header.evidence_id,
                        "section": section,
                        "section_digest": hashlib.sha256(text.encode()).hexdigest(),
                        "offset": offset,
                        "total_characters": len(text),
                        "content": chunk,
                    }
                    if section == "feedback":
                        frame.update(self._feedback_markers(index))
                    yield frame
        yield {"kind": "final", "registry_digest": self.registry.digest}

    def list_evidence(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        self._check_arguments(arguments, {"offset", "family_id", "case_id"})
        offset = _offset(arguments, total=self._collection_size(arguments))
        family = arguments.get("family_id")
        case_handle = arguments.get("case_id")
        if family is not None and case_handle is not None:
            raise ReaderArgumentError("mutually_exclusive_filters")
        if family is not None:
            if not isinstance(family, str):
                raise ReaderArgumentError("unknown_family")
            case_ids = self._family_case_ids.get(family)
            if case_ids is None:
                raise ReaderArgumentError("unknown_family")
            if offset > len(case_ids):
                raise ReaderArgumentError(
                    "invalid_offset", f"offset must be between 0 and {len(case_ids)}"
                )
            selected = case_ids[offset : offset + self.context_page_size]
            entries = [self._case_entry(case_id, "family_member") for case_id in selected]
            page, end = _bounded_entries(entries, 0, self.context_page_size)
            absolute_end = offset + end
            return {
                "entries": page,
                "total": len(case_ids),
                "next_offset": absolute_end if absolute_end < len(case_ids) else None,
            }
        elif case_handle is not None:
            case_id = self._resolve_case(case_handle)
            if case_id is None:
                detail = (
                    (
                        f"{json.dumps(case_handle, ensure_ascii=False)[:128]} is neither a case_handle nor a case_id; "
                        "use a case_handle or case_id from a list_evidence catalog entry"
                    )
                    if self.family15
                    else None
                )
                raise ReaderArgumentError("unknown_case", detail)
            indices = self._case_indices[case_id]
            if offset > len(indices):
                raise ReaderArgumentError(
                    "invalid_offset", f"offset must be between 0 and {len(indices)}"
                )
            record_indices = indices[offset : offset + self.context_page_size]
            entries = [self._record_entry(index) for index in record_indices]
            page, end = _bounded_entries(entries, 0, self.context_page_size)
            absolute_end = offset + end
            return {
                "entries": page,
                "total": len(indices),
                "next_offset": absolute_end if absolute_end < len(indices) else None,
            }
        else:
            if offset > len(self._case_ids):
                raise ReaderArgumentError(
                    "invalid_offset", f"offset must be between 0 and {len(self._case_ids)}"
                )
            selected = self._case_ids[offset : offset + self.context_page_size]
            entries = [self._case_entry(case_id, "catalog") for case_id in selected]
            page, end = _bounded_entries(entries, 0, self.context_page_size)
            absolute_end = offset + end
            return {
                "entries": page,
                "total": len(self._case_ids),
                "next_offset": absolute_end if absolute_end < len(self._case_ids) else None,
            }

    def _resolve_case(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        if value in self._handle_cases:
            return self._handle_cases[value]
        return value if self.family15 and value in self._case_indices else None

    def _collection_size(self, arguments: Mapping[str, Any]) -> int | None:
        family, case = arguments.get("family_id"), arguments.get("case_id")
        if family is not None and case is not None:
            return None
        if family is not None:
            ids = self._family_case_ids.get(family) if isinstance(family, str) else None
            return None if ids is None else len(ids)
        if case is not None:
            case_id = self._resolve_case(case)
            return None if case_id is None else len(self._case_indices[case_id])
        return len(self._case_ids)

    def read_evidence(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        self._check_arguments(arguments, {"evidence_id", "offset"}, required={"evidence_id"})
        evidence_id = arguments["evidence_id"]
        if not isinstance(evidence_id, str) or evidence_id not in self._evidence_index:
            raise ReaderArgumentError("unknown_evidence")
        index = self._evidence_index[evidence_id]
        body = {name: self._role_section(index, name) for name in self._section_names()}
        body.update(self._feedback_markers(index))
        return _page(_canonical(body), arguments.get("offset", 0))

    def list_documents(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        self._check_arguments(arguments, {"offset"})
        assert self._documents is not None
        page = _v2_catalog_page(self._documents, offset=arguments.get("offset", 0))
        if page.error is not None:
            raise ReaderArgumentError(
                page.error,
                f"offset must be between 0 and {page.total}"
                if page.error == "invalid_offset"
                else None,
            )
        return {
            "entries": [
                {
                    "document_id": item.document_id,
                    "path": item.path,
                    "title": item.title,
                    "size": item.size,
                    "digest": item.digest,
                }
                for item in page.entries
            ],
            "total": page.total,
            "next_offset": page.next_offset,
        }

    def read_document(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        self._check_arguments(arguments, {"document_id", "offset"}, required={"document_id"})
        document_id = arguments["document_id"]
        assert self._documents is not None
        page = _v2_read_document_page(
            self._documents, document_id=document_id, offset=arguments.get("offset", 0)
        )
        if page.error is not None:
            known = any(
                document.document_id == document_id for document in self._documents.documents
            )
            detail = (
                (
                    f"offset must be between 0 and {page.total_chars}"
                    if known
                    else "offset must be a non-negative integer"
                )
                if page.error == "invalid_offset"
                else None
            )
            raise ReaderArgumentError(page.error, detail)
        return {
            "document_id": page.document_id,
            "offset": page.offset,
            "total_characters": page.total_chars,
            "next_offset": page.next_offset,
            "content_digest": page.digest,
            "content": page.content,
        }

    def case_identity_summary(self, case_id: str) -> dict[str, object]:
        """A directly usable filter and a bounded display of the original case ID."""

        handle = self._case_handles[case_id]
        omitted = len(_canonical(case_id).encode("utf-8")) > SUMMARY_CASE_ID_BYTES
        return {
            "case_handle": handle,
            "case_id": handle if omitted else case_id,
            "case_id_omitted": omitted,
        }

    def cohort_summary(
        self, selected: PortionCohort, variants: Mapping[int, str]
    ) -> dict[str, object]:
        """Count case repeats separately from cohort signatures without reading bodies.

        Signature entries contain only a representative evidence ID and its
        multiplicity; arbitrary criterion and declared-variant strings stay in
        lazy evidence. Both retained ID lists are bounded with exact omissions.
        """

        cohort = selected.cohort
        case_headers = [self._headers[index] for index in self._case_indices[cohort.case_id]]
        signatures: dict[tuple[object, ...], list[_Header]] = {}
        outcomes: set[str] = set()
        criterion_sets: set[tuple[str, ...]] = set()
        declared: set[str] = set()
        unknown_criteria = 0
        for index in cohort.member_indices:
            header = self._headers[index]
            self._verify_header(header)
            outcomes.add(header.terminal)
            if header.failed_criteria is None:
                unknown_criteria += 1
            else:
                criterion_sets.add(header.failed_criteria)
            variant = variants.get(index)
            if variant is not None:
                declared.add(variant)
            signature = (header.terminal, header.critical, header.failed_criteria, variant)
            signatures.setdefault(signature, []).append(header)
        representatives = sorted(
            (
                (min(members, key=lambda row: (row.row.repeat, row.evidence_id)), len(members))
                for members in signatures.values()
            ),
            key=lambda item: (item[0].row.repeat, item[0].evidence_id),
        )
        exemplar_ids = {row.evidence_id for row in selected.exemplars}
        unrepresented = selected.unrepresented_variant_ids
        return {
            "repeat_summary": {
                "case_total": len(case_headers),
                "case_available": sum(header.available for header in case_headers),
                "case_passed": sum(
                    header.available and not header.failing for header in case_headers
                ),
                "case_unknown": sum(not header.available for header in case_headers),
                "cohort_failing": len(cohort.member_indices),
            },
            "variant_summary": {
                "distinct_outcomes": len(outcomes),
                "distinct_criterion_sets": len(criterion_sets),
                "unknown_criterion_repeats": unknown_criteria,
                "distinct_declared_variants": len(declared),
                "distinct_signatures": len(signatures),
                "entries": [
                    {
                        "evidence_id": header.evidence_id,
                        "repeats": count,
                        "represented": header.evidence_id in exemplar_ids,
                    }
                    for header, count in representatives[:SUMMARY_VARIANTS]
                ],
                "omitted_entries": max(0, len(representatives) - SUMMARY_VARIANTS),
            },
            "omitted_exemplar_count": len(cohort.member_indices) - len(selected.exemplars),
            "unrepresented_variant_ids": list(unrepresented[:SUMMARY_VARIANTS]),
            "omitted_unrepresented_variant_ids": max(0, len(unrepresented) - SUMMARY_VARIANTS),
        }

    def _case_entry(self, case_id: str, relation: str) -> dict[str, object]:
        available, failing, unknown = self._case_counts[case_id]
        return {
            **self.case_identity_summary(case_id),
            "relation": relation,
            "family_id": self._families.get(case_id),
            "available": available,
            "failing": failing,
            "unknown": unknown,
            "passed": sum(
                self._headers[index].available and not self._headers[index].failing
                for index in self._case_indices[case_id]
            ),
        }

    def _record_entry(self, index: int) -> dict[str, object]:
        header = self._headers[index]
        return {
            "evidence_id": header.evidence_id,
            "repeat": header.row.repeat,
            "available": header.available,
            "failing": header.failing,
            "relation": "observation",
        }

    def _role_observation_value(self, index: int, path: str) -> object:
        """A selected field as the Reflector sees it, without warming bodies
        for the authenticated header-only features used by default grouping.
        """
        tokens = _pointer_tokens(path)
        if tokens and tokens[0] in LazyObservationMapping._HEADER_FIELDS:
            return pointer_value(self._observation_header(index), path)
        return pointer_value(self._role_section(index, "observation"), path)

    def _role_section(self, index: int, section: str) -> object:
        """A section as the Reflector reads it through `read_evidence`.

        Family 14 renders the observation through the role predicate, cached
        under its own `{evidence_id}:observation:role` key, so a grouping read
        and a role read of the same record never share bytes in either order.
        Every other section, and every section of an earlier family, is the
        grouping section itself.
        """

        if section == "observation" and self._role_visible is not None:
            return self._section(index, section, role=True)
        return self._section(index, section)

    def _section(self, index: int, section: str, *, role: bool = False) -> object:
        assert not role or section == "observation"
        header = self._headers[index]
        self._verify_header(header)
        key = f"{header.evidence_id}:{section}:role" if role else f"{header.evidence_id}:{section}"
        cached = self._cache.get(key)
        if cached is not None:
            return json.loads(cached.decode("utf-8"))
        if section == "identity":
            value: object = {
                "case_id": header.row.case_id,
                "sample_id": header.row.sample_id,
                "repeat": header.row.repeat,
                "evidence_id": header.evidence_id,
            }
        elif section in {"case", "metadata"}:
            value = self._metadata.get(header.row.case_id, {})
        elif section == "public":
            value = {} if header.case is None else plain_public(header.case.public)
        elif section == "diagnostics":
            value = (
                None
                if not header.available
                else self._repeat_for(header.row, header.case).diagnostics
            )
        else:
            if not header.available:
                value = {} if section == "feedback" else self._unavailable_projection(header)
                if section == "feedback":
                    self._put_feedback_markers(header, 0, ())
            else:
                # Only the role path (`_role_section`, observation only) passes
                # the predicate; grouping and `_Sources` always read unfiltered.
                repeat = self._repeat_for(
                    header.row, header.case, visible=self._role_visible if role else None
                )
                if section == "feedback":
                    value = plain_public(repeat.feedback)
                    self._put_feedback_markers(
                        header, repeat.feedback_omitted, repeat.feedback_truncated
                    )
                else:
                    value = self._observation_projection(repeat)
        encoded = _canonical(value).encode("utf-8")
        self._cache.put(key, encoded)
        return value

    def _observation_header(self, index: int) -> dict[str, object]:
        """Return the small observation values authenticated at construction."""

        header = self._headers[index]
        self._verify_header(header)
        return {
            "terminal": header.terminal,
            "critical": header.critical,
            "failed_criteria": None
            if header.failed_criteria is None
            else list(header.failed_criteria),
        }

    def _repeat_for(
        self,
        row: SampleRow,
        case: EvalCase | None,
        *,
        visible: Callable[[str], bool] | None = None,
    ) -> RepeatEvidence:
        return _repeat_evidence(
            row,
            started=self._started,
            finished=self._finished,
            cases_by_id=self._cases,
            run_root=self._run_root,
            candidate=self._candidate,
            scorer=self._scorer if isinstance(self._scorer, Mapping) else {},
            environment_kind=self._environment_kind,
            metric_primary=self._metric_primary,
            limits=self._limits,
            diagnostics=self._scorer_diagnostics,
            visible=visible,
            traces=self._traces,
            secrets=self._secrets,
            pin=self.pin,
            hits=self._hits,
        )

    def _feedback_markers(self, index: int) -> dict[str, object]:
        header = self._headers[index]
        cached = self._cache.get(f"{header.evidence_id}:feedback:markers")
        if cached is not None:
            return json.loads(cached.decode("utf-8"))
        if not header.available:
            omitted = 0
            truncated: tuple[str, ...] = ()
        else:
            repeat = self._repeat_for(header.row, header.case)
            omitted = repeat.feedback_omitted
            truncated = repeat.feedback_truncated
        self._put_feedback_markers(header, omitted, truncated)
        return {"feedback_omitted": omitted, "feedback_truncated": list(truncated)}

    def _put_feedback_markers(
        self, header: _Header, omitted: int, truncated: Sequence[str]
    ) -> None:
        self._cache.put(
            f"{header.evidence_id}:feedback:markers",
            _canonical({"feedback_omitted": omitted, "feedback_truncated": list(truncated)}).encode(
                "utf-8"
            ),
        )

    def _unavailable_projection(self, header: _Header) -> dict[str, object]:
        return {
            "status": "unavailable",
            "terminal": header.terminal,
            "critical": header.critical,
            "primary_score": None
            if header.row.metrics is None
            else header.row.metrics.get(self._metric_primary),
            "verdicts": [],
            "failed_criteria": header.failed_criteria,
            "read_paths": [],
            "tool_names": [],
            "tool_outcomes": [],
            "answer_text": [],
            "behavior": {"status": "unavailable"},
        }

    def _observation_projection(self, repeat: RepeatEvidence) -> dict[str, object]:
        # The Reflector's serializer keeps the legacy audience boundary when
        # unpinned; the full view deliberately shares all turn fields (R8).
        payload = reflector_observation_payload(repeat.observation)
        read_paths: list[str] = []
        tool_names: list[str] = []
        tool_outcomes: list[str] = []
        answers: list[str] = []
        derived = reflector_observation_payload(
            replace(
                repeat.observation,
                full_turns=None,
                turns=repeat.observation.turns
                if repeat.observation.grouping_turns is None
                else repeat.observation.grouping_turns,
            )
        )
        for turn in derived["turns"]:
            for event in turn["events"]:
                if event["type"] == "skill_read":
                    read_paths.append(str(event["path"]))
                elif event["type"] == "tool_call":
                    tool_names.append(str(event["name"]))
                elif event["type"] == "tool_result":
                    tool_names.append(str(event["name"]))
                    tool_outcomes.append(str(event["status"]))
                elif event["type"] == "assistant_message":
                    answers.append(str(event["content"]))
        return {
            "status": repeat.trace,
            "terminal": repeat.outcome,
            "critical": repeat.critical_pass,
            "primary_score": repeat.primary_score,
            "verdicts": [dict(verdict) for verdict in repeat.verdicts],
            "failed_criteria": None
            if repeat.failed_criteria is None
            else list(repeat.failed_criteria),
            "read_paths": sorted(set(read_paths)),
            "tool_names": sorted(set(tool_names)),
            "tool_outcomes": sorted(set(tool_outcomes)),
            "answer_text": answers,
            "behavior": payload,
        }

    def _header_flags(
        self, row: SampleRow, case: EvalCase | None
    ) -> tuple[bool, bool, tuple[str, ...] | None, tuple[str, ...]]:
        """Read only the scorer terminal needed for immutable registry flags.

        The authenticated request/history projection is intentionally deferred
        to an observation or evidence read.  No full ``RepeatEvidence`` is
        retained while the catalog is constructed. The fourth value is every
        verdict criterion of an authenticated scorer output (empty otherwise),
        which feeds `published_criteria`.
        """
        started = self._started.get((row.case_id, row.sample_id))
        if started is None or case is None or self._candidate is None:
            return (
                False,
                row.critical_pass is False or (row.outcome or "not_started") != "completed",
                None,
                (),
            )
        request_id = started.payload.get("score_request_id")
        if not isinstance(request_id, str):
            return False, True, None, ()
        try:
            trace = read_trace(self._traces, started, ("request", "stdout"))
            # Authenticate the source binding and scorer output but deliberately
            # do not turn the history into its public behavior projection here.
            # Those potentially large bodies are projected only by `_section`.
            if (
                _bound_view(
                    None if trace is None else trace.request,
                    started,
                    request_id,
                    case,
                    self._candidate,
                    self._scorer,
                )
                is None
            ):
                return (
                    False,
                    row.critical_pass is False or (row.outcome or "not_started") != "completed",
                    None,
                    (),
                )
            text = decode_trace_text(None if trace is None else trace.stdout)
            result = _parse_score_result(
                parse_scorer_stdout(text), diagnostics=self._scorer_diagnostics
            )
            if criteria_carry_run_secret(
                result,
                self._secrets,
                pin=self.pin,
                hits=self._hits,
                location=f"criterion/{row.case_id}/{row.repeat}",
            ):
                return (
                    False,
                    row.critical_pass is False or (row.outcome or "not_started") != "completed",
                    None,
                    (),
                )
            failed_criteria = tuple(
                sorted(verdict["criterion"] for verdict in result.verdicts if not verdict["passed"])
            )
            criteria = tuple(sorted(verdict["criterion"] for verdict in result.verdicts))
        except ArtifactCorruptionError:
            raise
        except Exception:
            return (
                False,
                row.critical_pass is False or (row.outcome or "not_started") != "completed",
                None,
                (),
            )
        return (
            True,
            bool(failed_criteria)
            or row.critical_pass is False
            or (row.outcome or "not_started") != "completed",
            failed_criteria,
            criteria,
        )

    def _trace_fingerprints(self, row: SampleRow) -> tuple[tuple[str | None, str, str | None], ...]:
        started = self._started.get((row.case_id, row.sample_id))
        if started is None:
            return ()
        request_id = started.payload.get("score_request_id")
        if not isinstance(request_id, str):
            return ()
        return tuple(self._fingerprint(started, section) for section in ("request", "stdout"))

    def _fingerprint(self, started: Envelope, section: str) -> tuple[str | None, str, str | None]:
        """One section's `(location, section, sha256)`; each section is read alone,
        as each file was, so one unreadable section leaves the other's digest."""

        trace = read_trace(self._traces, started, (section,))
        data = None if trace is None else getattr(trace, section)
        if trace is None or data is None:
            return (None, section, None)
        return (trace.location, section, hashlib.sha256(data).hexdigest())

    def _verify_header(self, header: _Header) -> None:
        for location, section, expected in header.fingerprints:
            started = self._started.get((header.row.case_id, header.row.sample_id))
            current = (
                (None, section, None) if started is None else self._fingerprint(started, section)
            )
            if expected is None:
                if current[2] is None:
                    continue
                raise ReaderIntegrityError("source_changed")
            if current != (location, section, expected):
                raise ReaderIntegrityError("source_changed")

    @staticmethod
    def _check_arguments(
        arguments: Mapping[str, Any], allowed: set[str], *, required: set[str] | None = None
    ) -> None:
        if (
            not isinstance(arguments, Mapping)
            or set(arguments) - allowed
            or not (required or set()) <= set(arguments)
        ):
            raise ReaderArgumentError("invalid_arguments")
