"""Authenticated raw baseline episodes, redacted before character paging."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
from evolution.tree.artifacts.artifacts import ArtifactRef
from evolution.tree.execution.check_modes import HitSink
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.scorer_traces import ScorerTraceReader
from evolution.tree.roles.editor.editor_brief import _observation_evidence_id
from evolution.tree.roles.editor.editor_evidence import (
    CarriedTraces,
    _read_and_bind,
    _scoring_records,
    read_trace,
)
from evolution.tree.roles.input_secrets import input_secret_value
from evolution.tree.roles.reflection_reader import ReaderArgumentError, ReaderIntegrityError

from .storage.envelope import Envelope


class BaselineTraces:
    def __init__(
        self,
        *,
        traces: ScorerTraceReader,
        measurement_records: Sequence[Envelope],
        pin: EvidencePin,
        secrets: Sequence[str],
        corpus: Sequence[EvalCase],
        baseline: ArtifactRef,
        carried: CarriedTraces | None = None,
        hits: HitSink | None = None,
    ) -> None:
        self._hits = hits
        self._traces = traces
        self._pin = pin
        self._secrets = tuple(secrets)
        self._baseline = baseline
        self._cases = {case.case_id: case for case in corpus}
        identity, self._started, _ = _scoring_records(measurement_records, carried)
        self._scorer = identity.get("scorer", {})
        self._ids = {
            _observation_evidence_id(entry["case_id"], entry["repeat"]): (
                entry["case_id"],
                entry["sample_id"],
            )
            for entry in identity.get("schedule", ())
        }
        self._fingerprints = {id_: self._fingerprint(key) for id_, key in self._ids.items()}
        self._failed = False

    def with_hits(self, hits: HitSink) -> BaselineTraces:
        """Keep authenticated sources while attributing reads to a new operation."""
        reader = copy.copy(self)
        reader._hits = hits
        return reader

    def ids(self) -> tuple[str, ...]:
        return tuple(self._ids)

    def _fingerprint(self, key: tuple[str, str]) -> tuple[tuple[str | None, str | None], ...]:
        started = self._started.get(key)
        result = []
        for section in ("request", "stdout"):
            trace = None if started is None else read_trace(self._traces, started, (section,))
            data = None if trace is None else getattr(trace, section)
            result.append(
                (None, None)
                if trace is None or data is None
                else (trace.location, hashlib.sha256(data).hexdigest())
            )
        return tuple(result)

    def whole(self, evidence_id: str) -> str:
        if self._failed:
            raise ReaderIntegrityError("source_changed")
        if not isinstance(evidence_id, str) or evidence_id not in self._ids:
            raise ReaderArgumentError("unknown_evidence_id")
        key = self._ids[evidence_id]
        try:
            fingerprint = self._fingerprint(key)
        except ArtifactCorruptionError:
            raise
        except Exception as exc:
            raise ReaderArgumentError("trace_unavailable") from exc
        if fingerprint != self._fingerprints[evidence_id]:
            self._failed = True
            raise ReaderIntegrityError("source_changed")
        try:
            started = self._started.get(key)
            if started is None:
                raise ReaderArgumentError("trace_unavailable")
            trace = read_trace(self._traces, started, ("request",))
            if trace is None:
                raise ReaderArgumentError("trace_unavailable")
            self._baseline.verify()
            view = _read_and_bind(
                trace.request,
                started,
                str(started.payload["score_request_id"]),
                self._cases[key[0]],
                self._baseline.candidate_view(),
                self._scorer,
            )
            if view is None:
                raise ValueError("binding")
            return str(
                input_secret_value(
                    canonical_json(view).decode(),
                    self._secrets,
                    pin=self._pin,
                    hits=self._hits,
                    location=f"trace/{key[0]}/{key[1]}",
                )
            )
        except ArtifactCorruptionError:
            raise
        except Exception as exc:
            raise ReaderArgumentError("trace_unavailable") from exc

    def page(self, evidence_id: str, offset: int = 0) -> dict[str, Any]:
        text = self.whole(evidence_id)
        if type(offset) is not int or offset < 0 or offset > len(text):
            raise ReaderArgumentError("bad_offset", f"offset must be between 0 and {len(text)}")
        end = min(len(text), offset + self._pin.settings.trace_page_chars)
        return {
            "evidence_id": evidence_id,
            "content": text[offset:end],
            "content_digest": hashlib.sha256(text.encode()).hexdigest(),
            "offset": offset,
            "total_characters": len(text),
            "next_offset": end if end < len(text) else None,
        }

    def read(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if (
            not isinstance(arguments, Mapping)
            or set(arguments) - {"evidence_id", "offset"}
            or "evidence_id" not in arguments
        ):
            raise ReaderArgumentError("invalid_arguments")
        return self.page(arguments["evidence_id"], arguments.get("offset", 0))
