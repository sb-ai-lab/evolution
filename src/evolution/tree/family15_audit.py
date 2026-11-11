"""Read-only family-15 evidence authentication, separate from pure replay.

The later numerical producers must replace the explicit ``audit_incomplete``
findings below before public writer admission. Authentic bytes alone are not a
verified statistical decision, ranking or diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evolution.tree.artifacts.artifact_payloads import (
    AuxiliaryDeclaration,
    AuxiliarySourceReference,
    verify_directory,
)
from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
from evolution.tree.campaign_statistics import latest_rows
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json
from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle
from evolution.tree.family15_schema import decode_rational, validate_comparison_data
from evolution.tree.storage.append_log import _directory, _file, _read
from evolution.tree.storage.journal_errors import JournalFault

if TYPE_CHECKING:
    from evolution.tree.campaign_projections import _Evidence
    from evolution.tree.execution.interaction import EvalCase
    from evolution.tree.storage.envelope import Envelope


class EvidenceMismatch(ValueError):
    """A classified failed authentication; details never include local paths."""

    def __init__(self, detail: str, code: str = "family15_evidence_mismatch") -> None:
        super().__init__(detail)
        self.code = code


def _need(ok: object, detail: str) -> None:
    if not ok:
        raise EvidenceMismatch(detail)


def _read_owned(path: Path) -> bytes:
    directory = _directory(path.parent)
    try:
        fd = _file(directory, path.name, os.O_RDONLY)
        try:
            length = os.fstat(fd).st_size
            raw = _read(fd, length)
            _need(len(raw) == length, "evidence changed while reading")
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(directory)


class Family15Audit:
    """Per-audit caches and reference counts, using the authoritative L prefix."""

    def __init__(self, context: _Evidence, *, corpus: Sequence[EvalCase] | None = None) -> None:
        from evolution.tree.execution.journal_identity import corpus_digest

        self.context = context
        self.corpus = {} if corpus is None else {case.case_id: case for case in corpus}
        self.corpus_digest = None if corpus is None else corpus_digest(corpus)
        self.checked = 0
        self.preconditions = 0
        self.bodies: dict[str, dict[str, Any]] = {}
        self.snapshot = context.snapshot_before(len(context.records))
        self.journals: dict[str, dict[str, tuple[bytes, Envelope]]] = {}

    def physical_records(self, name: str) -> dict[str, tuple[bytes, Envelope]]:
        """Actual admitted line boundaries, without changing historical JSON parsing."""
        from evolution.tree.artifacts.artifact_ledger import MAX_LEDGER_RECORD_BYTES as LEDGER_LIMIT
        from evolution.tree.artifacts.artifact_schema import ARTIFACT_FAMILY, ArtifactLifecycle
        from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for
        from evolution.tree.execution.journal import MAX_RECORD_BYTES
        from evolution.tree.storage.append_log import _scan

        if name not in self.journals:
            raw = _read_owned(self.context.run_root / name)
            campaign = name == "campaign.jsonl"
            if campaign:
                log, _ = _scan(
                    raw,
                    MAX_RECORD_BYTES,
                    lambda: CampaignLifecycle(selector=None, family_version=15),
                    campaign_family_for(15),
                )
                _need(
                    log.records == self.context.records,
                    "campaign bytes changed after admitted-prefix inspection",
                )
            else:
                log, _ = _scan(raw, LEDGER_LIMIT, ArtifactLifecycle, ARTIFACT_FAMILY)
                _need(
                    log.journal_integrity == "complete",
                    "artifact ledger is not a complete admitted journal",
                )
            offset = 0
            found = {}
            for record in log.records:
                end = raw.index(b"\n", offset) + 1
                found[f"{name}#bytes={offset}:{end}"] = (raw[offset:end], record)
                offset = end
            self.journals[name] = found
        return self.journals[name]

    def ledger_publications(self) -> tuple[AuxiliaryPublication, ...]:
        """S2 authority includes physical terminal bytes, not reconstructed offsets."""
        declarations = {}
        publications = []
        for locator, (raw, record) in self.physical_records("artifacts.jsonl").items():
            if record.event == "auxiliary_publish_started":
                declarations[record.operation_id] = AuxiliaryDeclaration.from_payload(
                    record.payload
                )
            elif record.event == "auxiliary_publish_finished":
                publications.append(
                    AuxiliaryPublication(
                        declarations[record.operation_id],
                        AuxiliarySourceReference(
                            1,
                            "ledger_record",
                            record.to_payload()["run_id"],
                            locator,
                            hashlib.sha256(raw).hexdigest(),
                        ),
                    )
                )
        return tuple(publications)

    def reference(self, ref: Mapping[str, Any]) -> bytes:
        """Read only canonical local references; never follow a sibling symlink."""
        self.checked += 1
        match = re.fullmatch(
            r"(measurements/[^/#]+/events.jsonl)#bytes=0:([1-9][0-9]*)", str(ref["locator"])
        )
        _need(
            ref["kind"] == "journal_prefix" and match is not None,
            "expected local measurement prefix",
        )
        assert match is not None
        name, end = match[1], int(match[2])
        _need(".." not in name.split("/"), "unsafe measurement locator")
        raw = _read_owned(self.context.run_root / name)
        _need(end <= len(raw), "measurement prefix is missing")
        prefix = raw[:end]
        _need(
            hashlib.sha256(prefix).hexdigest() == ref["content_digest"],
            "measurement prefix digest differs",
        )
        self.context.cache[name] = raw
        return prefix

    @staticmethod
    def summary(measurement: Mapping[str, Any]) -> dict[str, Any]:
        return dict(
            measurement_id=measurement["measurement_id"],
            identity_digest=measurement["identity_digest"],
            candidate_digest=measurement["identity"]["candidate_digest"],
            prefix=measurement["prefix"],
        )

    def identity(self, mid: str, identity: Mapping[str, Any]) -> None:
        from evolution.tree.campaign_epochs import epoch_identity
        from evolution.tree.campaign_identity import derived_identity
        from evolution.tree.campaign_schema import side_of_entry

        entry = self.snapshot.measurements.get(mid)
        _need(entry is not None, "measurement is not bound to this campaign")
        assert entry is not None
        parsed = RunIdentityV1.from_payload(identity)
        _need(
            parsed.digest == entry["identity_digest"],
            "full measurement identity differs from campaign binding",
        )
        epoch = epoch_identity(self.snapshot, mid)
        expected = derived_identity(
            epoch,
            entry["candidate_digest"],
            "champion" if side_of_entry(entry) == "champion" else "candidate",
            case_subset=entry.get("case_subset"),
            case_repeats=entry.get("case_repeats"),
        )
        if entry.get("role") in {"family15_final", "family15_champion"}:
            from evolution.tree.campaign_identity import fresh_final_identity

            expected = fresh_final_identity(expected, mid, self.context.policy_for(mid).repeats)
        _need(
            parsed == expected,
            "measurement schedule, train set or condition identity differs from its epoch",
        )

    def measurement(
        self, measurement: Mapping[str, Any], *, complete: bool = False
    ) -> tuple[Any, ...]:
        """Match a full external identity to L and, when present, actual S1 bytes."""
        mid = str(measurement["measurement_id"])
        self.identity(mid, measurement["identity"])
        _need(
            RunIdentityV1.from_payload(measurement["identity"]).digest
            == measurement["identity_digest"],
            "external measurement identity digest differs",
        )
        if measurement["prefix"] is not None:
            return self.source(self.summary(measurement), complete=complete)
        _need(not complete, "completed measurement lacks a prefix")
        path = self.context.run_root / "measurements" / mid / "events.jsonl"
        try:
            raw = _read_owned(path)
        except FileNotFoundError:
            raw = b""
        if raw:
            created = json.loads(raw.splitlines()[0])
            _need(
                created["payload"]["identity"] == measurement["identity"],
                "born measurement differs from external identity",
            )
        return ()

    def source(self, source: Mapping[str, Any], *, complete: bool = True) -> tuple[Any, ...]:
        from evolution.tree.execution.journal import MAX_RECORD_BYTES
        from evolution.tree.storage.append_log import _scan

        mid = str(source["measurement_id"])
        ref = source["prefix"]
        _need(
            str(ref["locator"]).startswith(f"measurements/{mid}/events.jsonl#bytes="),
            "source prefix names another measurement",
        )
        raw = self.reference(ref)
        log, state = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
        _need(
            log.journal_integrity == "complete" and bool(log.records),
            "measurement prefix is not an admitted S1 journal",
        )
        created = log.records[0].to_payload()
        _need(created["run_id"] == ref["run_id"], "measurement reference belongs to another run")
        identity = created["payload"]["identity"]
        _need(
            created["payload"]["identity_digest"] == source["identity_digest"]
            and identity["candidate_digest"] == source["candidate_digest"],
            "source summary differs from actual measurement",
        )
        self.identity(mid, identity)
        if self.snapshot.measurements[mid].get("role") in {"family15_final", "family15_champion"}:
            self.fresh_imports(mid, log.records)
        assert isinstance(state, Lifecycle)
        rows = latest_rows(
            log.records,
            state,
            seed_mode=str(identity["target"]["seed_mode"]),
            dependency_digest=str(identity["target"]["dependency_digest"]),
        )
        if complete:
            _need(
                len(raw) == len(self.context.cache[f"measurements/{mid}/events.jsonl"]),
                "completed source has a later mutable measurement tail",
            )
            policy = self.context.policy_for(mid)
            _need(
                bool(rows)
                and all(
                    row.disposition == "score"
                    and row.metrics is not None
                    and policy.metric_primary in row.metrics
                    and 0 <= row.metrics[policy.metric_primary] <= 1
                    for row in rows
                ),
                "complete source lacks scheduled admissible coverage",
            )
        return rows

    def fresh_imports(self, mid: str, records: tuple[Envelope, ...]) -> None:
        """Only authorized own-fresh ancestry may carry scored observations."""
        from evolution.tree.baseline_import import (
            SourceMeasurement,
            admit_rows,
            import_pairs_digest,
        )
        from evolution.tree.campaign_projections import (
            _changed_top_fields,
            _derive_bound_identity,
            _opens_epoch,
        )
        from evolution.tree.campaign_schema import journal_citations
        from evolution.tree.execution.journal import MAX_RECORD_BYTES
        from evolution.tree.storage.append_log import _scan

        actual = [r.payload for r in records if r.event == "sample_imported"]
        rebound = next(
            (
                r
                for r in self.context.records
                if r.event == "measurement_rebound" and r.payload["measurement_id"] == mid
            ),
            None,
        )
        if rebound is None:
            _need(not actual, "fresh final measurement has undeclared imported scores")
            return
        entry = self.snapshot.measurements[mid]
        source_mid = rebound.payload["from_measurement_id"]
        source_entry = self.snapshot.measurements[source_mid]
        _need(
            all(
                source_entry.get(key) == entry.get(key)
                for key in ("role", "round", "attempt_id", "candidate_digest")
            )
            and source_entry["epoch"] < entry["epoch"],
            "fresh carry has another measurement's ancestry",
        )
        name = f"measurements/{source_mid}/events.jsonl"
        try:
            raw = _read_owned(self.context.run_root / name)
        except FileNotFoundError:
            raw = b""
        if not raw:
            # Missing and empty journals are both unborn under ADR-0020.
            # ADR-0020 permits a declared fresh measurement to move before birth.
            # Zero admissible rows alone do not prove this: stages, activity or
            # citations can establish that the missing frozen evidence existed.
            _need(
                not actual
                and rebound.payload["imported"] == 0
                and rebound.payload["pairs_digest"] == import_pairs_digest(())
                and rebound.payload["released"] == 0
                and not source_entry["stages"]
                and not source_entry.get("imported", 0),
                "unborn fresh source has recorded work or declared carry",
            )
            for row in self.context.records:
                _need(
                    not (
                        row.payload.get("measurement_id") == source_mid
                        and row.event not in {"champion_final_bound", "measurement_rebound"}
                    )
                    and not any(
                        isinstance(locator, str) and locator.startswith(f"{name}#bytes=")
                        for _, locator in journal_citations(row.event, row.payload)
                    ),
                    "unborn fresh source has recorded activity or a journal citation",
                )
            # Missing intermediate prebirth rebinds must authenticate their own
            # ancestry too. A deleted, uncited creation-only journal cannot be
            # distinguished from never-born using the existing durable facts.
            self.fresh_imports(source_mid, ())
            return
        frozen, _ = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
        _need(
            bool(frozen.records) and frozen.journal_integrity in {"complete", "incomplete_tail"},
            "frozen fresh source has no authentic readable prefix",
        )
        # R11: only the frozen ancestor may have an unrepaired torn tail. Its
        # valid physical prefix owns provenance; complete destination seals
        # still use source(..., complete=True)'s whole-journal check.
        raw = raw[: frozen.valid_bytes]
        created = frozen.records[0].to_payload()
        summary = dict(
            measurement_id=source_mid,
            identity_digest=source_entry["identity_digest"],
            candidate_digest=source_entry["candidate_digest"],
            prefix=dict(
                version=1,
                kind="journal_prefix",
                run_id=created["run_id"],
                locator=f"measurements/{source_mid}/events.jsonl#bytes=0:{len(raw)}",
                content_digest=hashlib.sha256(raw).hexdigest(),
            ),
        )
        # Recursive authentication reaches an original fresh binding with no
        # imported rows. Each hop is an admitted earlier-epoch own rebind.
        self.source(summary, complete=False)
        source_records, _ = self.context.measurement_state(summary["prefix"])
        fields = set()
        epoch = 0
        for row in self.context.records:
            if _opens_epoch(row, family_version=15):
                epoch += 1
                if source_entry["epoch"] < epoch <= entry["epoch"]:
                    fields.update(_changed_top_fields(row, family_version=15))
        source = SourceMeasurement(
            self.snapshot.run_id,
            source_mid,
            source_entry["identity_digest"],
            hashlib.sha256(raw).hexdigest(),
            source_records,
        )
        admitted = admit_rows(
            source,
            _derive_bound_identity(self.context, mid),
            primary=self.context.policy_for(mid).metric_primary,
            condition_changed=sorted(fields),
            primary_range=True,
        )
        _need(
            actual == list(admitted.rows)
            and rebound.payload["imported"] == admitted.imported
            and rebound.payload["pairs_digest"] == admitted.pairs_digest,
            "fresh carry imports differ from actual own source or condition provenance",
        )

    def data(self, record: Envelope, kind: str) -> dict[str, Any]:
        body = self.publication(
            AuxiliaryPublication.from_payload(record.payload["data"]),
            before=record.to_payload()["sequence"],
        )
        _need(body is not None and body["kind"] == kind, "event names another comparison body kind")
        assert body is not None
        return body

    def binding(self, record: Envelope) -> dict[str, Any]:
        p = record.payload
        body = self.data(record, "binding")
        for name in ("candidate", "reference"):
            _need(
                self.summary(body[name]) == p[name],
                "binding summary differs from external identity",
            )
            self.measurement(body[name])
        for name in ("epoch", "reference_kind"):
            _need(body[name] == p[name], "binding event differs from external body")
        policy = self.context.policy_for(body["candidate"]["measurement_id"])
        _need(
            body["policy_digest"] == policy.digest and body["mode"] == policy.gate_mode,
            "comparison policy differs from bound epoch",
        )
        from evolution.tree.campaign_schema import confidence_scopes

        round_state = self.context.snapshot_before(record.to_payload()["sequence"] - 1).rounds[
            p["round"]
        ]
        attempt = round_state.attempts[p["attempt_id"]]
        corpus = self.context.identity_for(body["candidate"]["measurement_id"]).corpus_cases
        assert attempt.selection is not None and round_state.gate_scope is not None
        focus, causal = confidence_scopes(
            attempt.selection, corpus, round_state.gate_scope["cases"]
        )
        _need(
            [s["declared_cases"] for s in body["scopes"]]
            == [[corpus[i]["case_id"] for i in indexes] for indexes in (focus, causal)],
            "comparison scope differs from admitted focus/affected train cases",
        )
        decision = None if attempt.family15 is None else attempt.family15.focus_decision
        if decision is not None:
            _need(
                p.get("focus")
                == dict(
                    seq=decision.envelope.to_payload()["sequence"],
                    digest=decision.reference.content_digest,
                    outcome="passed",
                ),
                "replacement binding changes carried focus",
            )
            _need(decision.payload["outcome"] == "passed", "replacement carries nonpassing focus")
        else:
            _need("focus" not in p, "initial binding carries unknown focus")
        self.reference_profile(body)
        return body

    def reference_profile(self, body: Mapping[str, Any]) -> None:
        """Authenticate retained/excluded slots and stored numbers before arithmetic."""
        reference = body["reference"]
        mid = reference["measurement_id"]
        corpus = self.context.identity_for(mid).corpus_cases
        groups = {c["case_id"]: c["group_id"] for c in corpus}
        rows = self.measurement(reference) if reference["prefix"] is not None else ()
        by_slot = {(row.case_id, row.repeat): row for row in rows}
        primary = self.context.policy_for(mid).metric_primary
        excluded = {(s["case_id"], s["repeat"]) for s in body["excluded_reference"]}
        if body["reference_kind"] == "stored":
            actual = {
                (row.case_id, row.repeat)
                for row in rows
                if row.exhausted and row.disposition != "score"
            }
            _need(excluded == actual, "stored exclusions differ from actual settled reference rows")
        for scope in body["scopes"]:
            retained = {c["case_id"]: c for c in scope["cases"]}
            schedule = reference["identity"]["schedule"]
            expected = {
                (s["case_id"], s["repeat"])
                for s in schedule
                if s["case_id"] in scope["declared_cases"]
                and (s["case_id"], s["repeat"]) not in excluded
            }
            _need(
                {(c["case_id"], r) for c in retained.values() for r in c["repeats"]} == expected,
                "scope omits or adds retained scheduled reference slots",
            )
            for case in retained.values():
                _need(
                    groups.get(case["case_id"]) == case["group"],
                    "scope group differs from train corpus",
                )
                if body["reference_kind"] == "stored":
                    from fractions import Fraction

                    actual_values = []
                    for repeat in case["repeats"]:
                        row = by_slot[(case["case_id"], repeat)]
                        _need(
                            row.disposition == "score"
                            and row.metrics is not None
                            and primary in row.metrics
                            and 0 <= row.metrics[primary] <= 1,
                            "stored scope includes an inadmissible reference slot",
                        )
                        actual_values.append(Fraction(row.metrics[primary]))
                    _need(
                        list(map(decode_rational, case["reference_values"])) == actual_values,
                        "stored reference values differ from actual scorer rows",
                    )

    def seal(self, record: Envelope) -> dict[str, Any]:
        p = record.payload
        body = self.data(record, "gate_seal")
        _need(
            self.summary(body["measurement"]) == p["source"]
            and body["slots_digest"] == p["slots_digest"],
            "gate seal differs from external source or slots",
        )
        self.measurement(body["measurement"], complete=True)
        attempt = self.snapshot.rounds[p["round"]].attempts[p["attempt_id"]]
        assert attempt.family15 is not None
        completed = attempt.family15.gate_candidate
        _need(
            completed is not None and completed.payload["source"] == p["source"],
            "seal is not its own completed Candidate gate",
        )
        return body

    def completion(self, record: Envelope) -> dict[str, Any]:
        from evolution.tree.baseline_import import SourceMeasurement
        from evolution.tree.execution.journal import MAX_RECORD_BYTES
        from evolution.tree.storage.append_log import _scan

        p = record.payload
        body = self.data(record, "completion")
        for name in ("gate_seal", "finalist_closure"):
            _need(body[name] == p[name], "completion source reference differs from campaign")
        seal = self.campaign_record(body["gate_seal"], record.to_payload()["sequence"])
        _need(seal.event == "candidate_gate_sealed", "completion cites another seal kind")
        _need(
            self.seal(seal)["measurement"] == body["source"], "completion changed its sealed source"
        )
        closure = self.campaign_record(body["finalist_closure"], record.to_payload()["sequence"])
        _need(
            closure.event == "attempt_closed"
            and closure.payload["outcome"] == "finalist"
            and closure.payload["attempt_id"] == p["attempt_id"],
            "completion source is not its eligible finalist",
        )
        destination = body["destination"]
        _need(
            destination["measurement_id"] == p["measurement_id"]
            and destination["identity_digest"] == p["identity_digest"],
            "completion destination differs",
        )
        self.measurement(destination)
        self.measurement(body["source"], complete=True)
        raw = self.reference(body["source"]["prefix"])
        log, _ = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
        source = SourceMeasurement(
            self.snapshot.run_id,
            body["source"]["measurement_id"],
            body["source"]["identity_digest"],
            hashlib.sha256(raw).hexdigest(),
            log.records,
            valid_bytes=len(raw),
        )
        # Preserve both transitive row provenance and condition epochs crossed
        # since this completed source was first measured (ADR-0017/0020).
        old = self.snapshot.measurements[source.measurement_id]["epoch"]
        new = self.snapshot.measurements[p["measurement_id"]]["epoch"]
        fields: set[str] = set()
        epoch = 0
        from evolution.tree.campaign_projections import _changed_top_fields, _opens_epoch

        for row in self.context.records:
            if _opens_epoch(row, family_version=15):
                epoch += 1
                if old < epoch <= new:
                    fields.update(_changed_top_fields(row, family_version=15))
        from evolution.tree.baseline_import import FrozenSourceCorrupt
        from evolution.tree.candidate_completion import (
            MeasurementBinding,
            prepare_candidate_completion,
        )

        try:
            imported = prepare_candidate_completion(
                source,
                MeasurementBinding(
                    p["measurement_id"],
                    RunIdentityV1.from_payload(destination["identity"]),
                    self.context.policy_for(p["measurement_id"]).metric_primary,
                    tuple(sorted(fields)),
                ),
                gate_seal=self.seal(seal),
            )
        except FrozenSourceCorrupt as exc:
            raise EvidenceMismatch("completion sealed source admission failed") from exc

        _need(
            list(imported.rows) == body["rows"] and imported.declaration() == body["imports"],
            "completion imports differ from actual sealed source rows or provenance",
        )
        _need(
            p["pairs_digest"] == imported.pairs_digest
            and p["imported"] == imported.imported
            and p["required"] == imported.scheduled
            and p["missing_count"] == len(body["missing_slots"]),
            "completion bounded counts differ from authenticated rows",
        )
        path = self.context.run_root / "measurements" / p["measurement_id"] / "events.jsonl"
        if path.exists():
            raw = _read_owned(path)
            log, _ = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
            _need(log.journal_integrity == "complete", "completion journal is corrupt")
            actual = [row.payload for row in log.records if row.event == "sample_imported"]
            _need(
                actual == body["rows"],
                "born completion imports differ from sealed source declaration",
            )
        return body

    def source_identity(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """Identity from the same authenticated prefix, including a current rebound."""
        raw = self.reference(source["prefix"])
        return dict(json.loads(raw.splitlines()[0])["payload"]["identity"])

    def result(self, record: Envelope) -> dict[str, Any]:
        from evolution.tree.campaign_policy import CampaignPolicyV12

        p = record.payload
        body = self.data(record, "result")
        bound = self.campaign_record(p["binding"], record.to_payload()["sequence"])
        global_result = record.event == "global_comparison_completed"
        binding = self.final(bound)["comparison"] if global_result else self.binding(bound)
        policy = self.context.policy_for(binding["candidate"]["measurement_id"])
        assert isinstance(policy, CampaignPolicyV12)
        if not policy.focus_first:
            _need(
                all(scope["status"] != "not_measured" for scope in body["scopes"]),
                "single comparison cannot leave causal unmeasured",
            )
            _need(
                body.get("bootstrap", {}).get("scope", "joint") == "joint",
                "single comparison cannot use a stage seed",
            )
        validate_comparison_data(
            body,
            binding=binding,
            policy=policy if policy.bound_fallback is not None or policy.focus_first else None,
        )
        _need(
            body["binding_digest"] == hashlib.sha256(canonical_json(binding)).hexdigest(),
            "result names another full comparison binding",
        )
        focus_decision = record.event == "focus_decided"
        if focus_decision:
            _need(
                policy.focus_first
                and body["deciding_scope"] == "focus"
                and [scope["name"] for scope in body["scopes"]] == ["focus"],
                "focus decision contains another scope",
            )
            for key in ("outcome", "reason"):
                _need(body[key] == p[key], "focus data differs from campaign verdict")
            _need(body.get("bootstrap") == p.get("seed"), "focus seed differs from its data")
        else:
            for key in ("candidate", "reference", "outcome", "reason", "deciding_scope"):
                _need(body[key] == p[key], "comparison result differs from bounded campaign record")
        focus_only = focus_decision or any(
            scope["status"] == "not_measured" for scope in body["scopes"]
        )
        focus_cases = set(binding["scopes"][0]["declared_cases"]) if focus_only else None
        if focus_cases is not None:
            _need(
                all(item["case_id"] in focus_cases for item in body["repeat_diagnostics"]),
                "focus diagnostics include causal cases",
            )
        if policy.focus_first and not focus_decision and not global_result:
            before = self.context.snapshot_before(record.to_payload()["sequence"] - 1)
            attempt = before.rounds[p["round"]].attempts[p["attempt_id"]]
            assert attempt.family15 is not None
            decision = attempt.family15.focus_decision
            _need(decision is not None, "comparison lacks durable focus")
            assert decision is not None
            recorded_focus = self.result(decision.envelope)
            _need(
                body["scopes"][0] == recorded_focus["scopes"][0],
                "comparison changed recorded focus result",
            )
            _need(
                focus_only == (decision.payload["outcome"] != "passed"),
                "comparison owes wrong scopes",
            )
        _need(body["mode"] == binding["mode"], "result mode differs from binding")
        for side in ("candidate", "reference"):
            summary = self.summary(binding[side])
            if global_result:
                before = self.context.snapshot_before(record.to_payload()["sequence"] - 1)
                assert before.family15 is not None
                current = before.family15.current_measurements.get(
                    summary["measurement_id"], summary["measurement_id"]
                )
                entry = before.measurements[current]
                summary = dict(
                    summary,
                    measurement_id=current,
                    identity_digest=entry["identity_digest"],
                    candidate_digest=entry["candidate_digest"],
                )
            _need(
                {**summary, "prefix": body[side]["prefix"]} == body[side],
                "result source differs from bound identity",
            )
            complete = side == "candidate" or binding["reference_kind"] == "fresh"
            rows = self.source(body[side], complete=complete and not focus_only)
            if focus_cases is not None:
                rows = tuple(row for row in rows if row.case_id in focus_cases)
                expected = {
                    (slot["case_id"], slot["repeat"])
                    for slot in binding[side]["identity"]["schedule"]
                    if slot["case_id"] in focus_cases
                }
                _need(
                    {(row.case_id, row.repeat) for row in rows} == expected,
                    "focus source omits or adds frozen slots",
                )
                if complete:
                    from evolution.tree.case_comparison import admissible

                    _need(
                        all(admissible(row, policy.metric_primary) for row in rows),
                        "focus source lacks complete admissible rows",
                    )
                    records, _ = self.context.measurement_state(body[side]["prefix"])
                    _need(
                        all(
                            row.event != "rollout_started" or row.payload["case_id"] in focus_cases
                            for row in records
                        ),
                        "causal row dispatched before focus passed",
                    )
            primary = self.context.policy_for(body[side]["measurement_id"]).metric_primary
            coverage = dict(
                required=len(rows),
                settled=sum(row.disposition == "score" or row.exhausted for row in rows),
                admissible=sum(
                    row.disposition == "score"
                    and row.metrics is not None
                    and primary in row.metrics
                    and 0 <= row.metrics[primary] <= 1
                    for row in rows
                ),
            )
            _need(
                body["coverage"][side] == coverage,
                "result coverage differs from actual scheduled rows",
            )
        _need(
            body["excluded_reference"] == binding["excluded_reference"],
            "result changed bound reference exclusions",
        )
        schedules = {
            side: {(s["case_id"], s["repeat"]): s for s in binding[side]["identity"]["schedule"]}
            for side in ("candidate", "reference")
        }
        if global_result:
            schedules = {
                side: {
                    (slot["case_id"], slot["repeat"]): slot
                    for slot in self.source_identity(body[side])["schedule"]
                }
                for side in ("candidate", "reference")
            }
        retained = {
            (case["case_id"], repeat)
            for scope in binding["scopes"]
            if not focus_only or scope["name"] == "focus"
            for case in scope["cases"]
            for repeat in case["repeats"]
        }
        actual = set()
        for pair in body["retained_pairs"]:
            slot_key = pair["candidate"]["case_id"], pair["candidate"]["repeat"]
            _need(
                slot_key not in actual
                and all(schedules[side].get(slot_key) == pair[side] for side in schedules),
                "result pair differs from frozen schedule",
            )
            actual.add(slot_key)
        _need(actual == retained, "result retained pairs differ from binding")
        bound_scopes = binding["scopes"][:1] if focus_decision else binding["scopes"]
        _need(
            [s["name"] for s in body["scopes"]] == [s["name"] for s in bound_scopes],
            "result scopes differ from binding",
        )
        for result, scope in zip(body["scopes"], bound_scopes, strict=True):
            _need(
                result["declared_count"] == len(scope["declared_cases"]),
                "result scope coverage differs from binding",
            )
            # The schema above owns the sole R8 rule-substitution exception.
            if result["rule"] == "bound":
                _need(result["confidence"] == scope["confidence"], "result confidence differs")
        return body

    def final(self, record: Envelope) -> dict[str, Any]:
        _need(record.event == "final_measurement_bound", "global result cites another binding kind")
        p = record.payload
        body = self.data(record, "final")
        for key in (
            "verification",
            "candidate_source",
            "champion_side",
            "finalist_closure",
            "source_ref",
        ):
            _need(body[key] == p[key], "final body differs from bounded campaign binding")
        for side in ("candidate", "champion"):
            _need(
                self.summary(body[side]) == p[side],
                "final measurement summary differs from full identity",
            )
            self.measurement(body[side])
            assert self.snapshot.family15 is not None
            original = body[side]["measurement_id"]
            current = self.snapshot.family15.current_measurements.get(original, original)
            if current != original:
                from evolution.tree.campaign_projections import _derive_bound_identity

                identity = _derive_bound_identity(self.context, current)
                self.measurement(
                    dict(
                        measurement_id=current,
                        identity=identity.to_payload(),
                        identity_digest=identity.digest,
                        prefix=None,
                    )
                )
        comparison = body["comparison"]
        epoch = self.context.identity_for(body["candidate"]["measurement_id"])
        _need(
            comparison["scopes"][0]["declared_cases"] == [c["case_id"] for c in epoch.corpus_cases],
            "final scope is not the whole train corpus",
        )
        self.reference_profile(comparison)
        closure = self.campaign_record(body["finalist_closure"], record.to_payload()["sequence"])
        _need(
            closure.event == "attempt_closed"
            and closure.payload["outcome"] == "finalist"
            and closure.payload["attempt_id"] == p["attempt_id"],
            "final has no own eligible gate closure",
        )
        source = self.campaign_record(body["source_ref"], record.to_payload()["sequence"])
        if p["verification"] == "fresh":
            self.seal(source)
        else:
            if source.event == "candidate_completion_completed":
                source = self.campaign_record(
                    source.payload["binding"], source.to_payload()["sequence"]
                )
            _need(
                source.event == "candidate_completion_bound", "reuse final has no completion source"
            )
            completion = self.completion(source)
            imported = self.completion_carry_at(source, completion, record, body["candidate"])
            carried = {(row["case_id"], row["sample_id"]) for row in imported}
            missing = [
                slot
                for slot in body["candidate"]["identity"]["schedule"]
                if (slot["case_id"], slot["sample_id"]) not in carried
            ]
            _need(
                body["candidate_work"] == missing,
                "reuse final dispatches other than missing destination-only slots",
            )
        if p["verification"] == "fresh":
            _need(
                body["candidate_work"] == body["candidate"]["identity"]["schedule"],
                "fresh final must schedule every Candidate slot",
            )
        order = [
            dict(measurement_id=body["candidate"]["measurement_id"], slot=slot)
            for slot in body["candidate_work"]
        ]
        if body["champion_side"] == "fresh":
            order.extend(
                dict(measurement_id=body["champion"]["measurement_id"], slot=slot)
                for slot in body["champion"]["identity"]["schedule"]
            )
            order.sort(
                key=lambda row: (
                    row["slot"]["case_id"],
                    row["slot"]["repeat"],
                    row["measurement_id"],
                )
            )
        _need(order == body["schedule_order"], "final interleaving differs from frozen work")
        return body

    def completion_carry_at(
        self,
        binding: Envelope,
        completion: Mapping[str, Any],
        final: Envelope,
        candidate: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        """Authenticate the original completion and each carry before this final binding."""
        from evolution.tree.baseline_import import (
            FrozenSourceCorrupt,
            admit_rows,
            fields_between,
            read_frozen_source,
        )
        from evolution.tree.campaign_projections import _derive_bound_identity

        original = binding.payload["measurement_id"]
        current = self.context.current(original, before=final)
        _need(current == candidate["measurement_id"], "final names another completion lineage")
        rows = tuple(completion["rows"])
        chain = tuple(reversed(self.context.chain(current)))
        _need(chain[0] == original, "completion ancestry has another original binding")
        for old, new in zip(chain[:-1], chain[1:], strict=True):
            try:
                source = read_frozen_source(
                    self.context.run_root,
                    self.snapshot.run_id,
                    old,
                    expected_identity_digest=self.snapshot.measurements[old]["identity_digest"],
                )
            except FrozenSourceCorrupt as exc:
                raise EvidenceMismatch("completion carry source cannot be authenticated") from exc
            self.identity(old, source.records[0].payload["identity"])
            _need(
                [r.payload for r in source.records if r.event == "sample_imported"] == list(rows),
                "completion carry source imports differ from its declaration",
            )
            admitted = admit_rows(
                source,
                _derive_bound_identity(self.context, new),
                primary=self.context.policy_for(new).metric_primary,
                condition_changed=fields_between(self.snapshot, old, new),
                primary_range=True,
            )
            rebound = self.context.rebinds()[new][1]
            _need(
                rebound.payload["imported"] == admitted.imported
                and rebound.payload["pairs_digest"] == admitted.pairs_digest,
                "completion carry differs from actual source admission",
            )
            rows = admitted.rows
        return rows

    def ranking(self, record: Envelope) -> dict[str, Any]:
        p = record.payload
        body = self.data(record, "ranking")
        round_state = self.context.snapshot_before(record.to_payload()["sequence"] - 1).rounds[
            p["round"]
        ]
        expected = sorted(
            (
                a
                for a in round_state.attempts.values()
                if a.family15 is not None and a.family15.global_result is not None
            ),
            key=lambda a: a.index,
        )
        _need(
            [entry["attempt_id"] for entry in body["entries"]] == [a.attempt_id for a in expected],
            "ranking omits or adds settled finalists",
        )
        policy = (
            self.context.policy_for(body["entries"][0]["evidence"]["measurement_id"])
            if body["entries"]
            else self.context.policy()
        )
        _need(body["round_stop"] == policy.round_stop, "ranking uses another round-stop policy")
        for entry, attempt in zip(body["entries"], expected, strict=True):
            result = self.campaign_record(entry["result"], record.to_payload()["sequence"])
            assert attempt.family15 is not None
            actual = attempt.family15.global_result
            assert actual is not None
            _need(
                result.encode() == actual.envelope.encode(),
                "ranking references another finalist result",
            )
            self.result(result)
            _need(
                entry["evidence"] == result.payload["candidate"]
                and entry["accepted"] == result.payload["accepted"]
                and entry["attempt_index"] == attempt.index
                and entry["candidate_digest"] == attempt.candidate_digest,
                "ranking entry differs from authenticated result",
            )
        if self.context.recompute:
            from evolution.tree.case_comparison import full_primary_score, ranking_payload

            for entry in body["entries"]:
                mid = entry["evidence"]["measurement_id"]
                point = full_primary_score(
                    self.source(entry["evidence"]),
                    self.source_identity(entry["evidence"])["schedule"],
                    corpus=self.context.identity_for(mid).corpus_cases,
                    primary=self.context.policy_for(mid).metric_primary,
                )
                _need(point == entry["point"], "ranking differs from full exact Candidate score")
            _need(
                ranking_payload(body["entries"], body["round_stop"]) == body,
                "ranking winner differs from full score and Attempt tie break",
            )
        if p["selected_candidate"] is not None:
            winner = next(
                (e for e in body["entries"] if e["attempt_id"] == body["proposed_attempt"]), None
            )
            _need(
                winner is not None
                and winner["candidate_digest"] == p["selected_candidate"]
                and winner["evidence"] == p["selected_evidence"],
                "selection differs from ranking proposal",
            )
        return body

    def campaign_record(self, ref: Mapping[str, Any], before: int | None = None) -> Envelope:
        self.checked += 1
        _need(self.context.family_version == 15, "actual campaign family is not 15")
        _need(ref["kind"] == "journal_record", "expected campaign record reference")
        located = self.physical_records("campaign.jsonl").get(str(ref["locator"]))
        _need(located is not None, "campaign reference is dangling or not a whole admitted record")
        assert located is not None
        raw, record = located
        _need(
            record.to_payload()["run_id"] == ref["run_id"],
            "campaign reference belongs to another run",
        )
        _need(
            hashlib.sha256(raw).hexdigest() == ref["content_digest"],
            "campaign reference digest differs",
        )
        _need(
            before is None or record.to_payload()["sequence"] < before,
            "campaign reference is not prior",
        )
        return record

    def validate_data(self, body: dict[str, Any], *, before: int | None = None) -> None:
        """Validate new scope data against journal-owned policy and binding context.

        Legacy shape-only auxiliary reads remain supported. New fallback and
        measured-noise data never manufacture policy from their own declarations.
        A result may be published before its consuming event (crash recovery),
        so locate its already-consumed binding by exact digest in the prior log.
        """
        from evolution.tree.campaign_policy import CampaignPolicyV12

        if not isinstance(body, dict):
            validate_comparison_data(body)
            return
        scopes = body.get("scopes", ())
        contextual = (
            any(
                isinstance(s, dict)
                and (
                    "fallback" in s
                    or s.get("tolerance") == "measured_noise"
                    or (body.get("kind") == "result" and "threshold" in s)
                )
                for s in scopes
            )
            if isinstance(scopes, list)
            else False
        )
        if not contextual and "calibration" not in body and "calibration_unavailable" not in body:
            validate_comparison_data(body)
            return
        limit = len(self.context.records) + 1 if before is None else before
        binding = None
        if body.get("kind") == "result":
            for record in self.context.records[: limit - 1]:
                if record.event not in {"comparison_bound", "final_measurement_bound"}:
                    continue
                candidate = self.data(
                    record, "binding" if record.event == "comparison_bound" else "final"
                )
                if record.event == "final_measurement_bound":
                    candidate = candidate["comparison"]
                if hashlib.sha256(canonical_json(candidate)).hexdigest() == body["binding_digest"]:
                    binding = candidate
                    break
            _need(binding is not None, "result lacks its exact prior comparison binding")
            assert binding is not None
            policy = self.context.policy_for(binding["candidate"]["measurement_id"])
        elif body.get("kind") == "preflight":
            from evolution.tree.campaign_policy import policy_class_for_payload

            epochs = self.context.snapshot_before(limit - 1).epochs
            epoch = body["epoch"]
            _need(type(epoch) is int and 0 <= epoch < len(epochs), "preflight epoch is not bound")
            raw = epochs[epoch]["policy"]
            policy = policy_class_for_payload(raw).from_payload(raw)
        else:
            policy = self.context.policy_for(body["candidate"]["measurement_id"])
        _need(
            isinstance(policy, CampaignPolicyV12), "comparison requires its bound family-15 policy"
        )
        assert isinstance(policy, CampaignPolicyV12)
        validate_comparison_data(body, policy=policy, binding=binding)

    def publication(
        self, publication: AuxiliaryPublication, *, before: int | None = None
    ) -> dict[str, Any] | None:
        declaration = publication.declaration
        record = self.campaign_record(declaration.declaration_ref.to_payload(), before)
        _need(
            record.event == "auxiliary_payload_declared", "publication has no campaign declaration"
        )
        expected = declaration.to_payload()
        expected.pop("declaration_ref")
        _need(record.payload == expected, "publication differs from campaign declaration")
        self.checked += 1
        if publication not in self.ledger_publications():
            raise EvidenceMismatch(
                "publication does not name actual local S2 terminal bytes",
                "family15_artifact_integrity",
            )
        verify_directory(self.context.run_root / publication.locator, declaration, committed=True)
        if declaration.purpose == "baseline_diagnostics":
            return None  # Bundle contents are authenticated by the store; Task11 owns semantics.
        key = declaration.digest
        if key not in self.bodies:
            raw = _read_owned(self.context.run_root / publication.locator / "payload.json")
            _need(
                hashlib.sha256(raw).hexdigest() == declaration.content_digest,
                "payload changed after publication verification",
            )
            try:
                body = json.loads(raw)
                _need(canonical_json(body) == raw, "comparison data is not canonical JSON")
                self.validate_data(body, before=record.to_payload()["sequence"])
            except (ValueError, TypeError, KeyError) as exc:
                raise EvidenceMismatch(
                    "invalid canonical comparison-data body", "family15_payload_invalid"
                ) from exc
            self.bodies[key] = body
        return self.bodies[key]

    def declaration(self, record: Envelope) -> None:
        publications = self.ledger_publications()
        physical = next(
            raw
            for raw, admitted in self.physical_records("campaign.jsonl").values()
            if admitted.to_payload()["sequence"] == record.to_payload()["sequence"]
        )
        digests = {
            hashlib.sha256(physical).hexdigest(),
            hashlib.sha256(record.encode()).hexdigest(),
        }
        found = [p for p in publications if p.declaration.declaration_ref.content_digest in digests]
        if not found:
            if self.abandoned_diagnostic_debt(record):
                return
            raise EvidenceMismatch(
                "declared auxiliary publication is not committed", "family15_publication_pending"
            )
        _need(len(found) == 1, "declaration has duplicate publications")
        self.publication(found[0])

    def abandoned_diagnostic_debt(self, record: Envelope) -> bool:
        """Only proven uncommitted linked-import debt is released by fresh_baseline."""
        if self.snapshot.baseline_abandoned is None:
            return False
        creation = self.context.records[0].payload["identity"]
        linked = creation.get("linked_from") or {}
        baseline = linked.get("baseline")
        if baseline is None:
            return False
        prior = self.context.records[: record.to_payload()["sequence"] - 1]
        if any(r.event == "measurement_bound" for r in prior):
            return False
        diagnostics = [
            r
            for r in self.context.records
            if r.event == "auxiliary_payload_declared"
            and r.payload["purpose"] == "baseline_diagnostics"
        ]
        # This producer has exactly one linked-import bundle, before any S1
        # binding. The existing fresh_baseline authorization releases that
        # original scalar import. No stronger origin proof is invented for an
        # unpublished bundle whose descriptor carries only content hashes.
        if len(diagnostics) != 1:
            return False
        abandoned = [
            r
            for r in self.context.records
            if r.event == "authorization_applied"
            and r.payload.get("effect") == "fresh_baseline"
            and r.payload.get("pairs_digest") == baseline["pairs_digest"]
        ]
        if len(abandoned) != 1:
            return False
        if record.payload["purpose"] == "baseline_diagnostics":
            return diagnostics[0] == record
        from evolution.tree.baseline_diagnostics import read_prepared_binding

        try:
            body = read_prepared_binding(
                self.context.run_root,
                digest=record.payload["content_digest"],
                byte_count=record.payload["files"][0]["byte_count"],
            )
        except FileNotFoundError:
            return False
        _need(
            record.payload["files"]
            == [
                dict(
                    name="payload.json",
                    byte_count=len(canonical_json(body)),
                    digest=record.payload["content_digest"],
                )
            ],
            "abandoned diagnostic preparation differs from declared file",
        )
        validate_comparison_data(body)
        if body["kind"] != "diagnostic_import":
            return False
        from evolution.tree.campaign_identity import (
            CampaignIdentityV1,
            derived_identity,
            measurement_id_for,
        )

        expected = derived_identity(
            CampaignIdentityV1.from_payload(creation),
            creation["artifact_store"]["seed_candidate_digest"],
            "champion",
        )
        _need(
            body["rows_digest"] == baseline["pairs_digest"]
            and body["destination"]["identity"] == expected.to_payload()
            and body["destination"]["measurement_id"] == measurement_id_for(None),
            "abandoned diagnostic proof differs from original baseline",
        )
        bundles = [
            r
            for r in prior
            if r.event == "auxiliary_payload_declared"
            and r.payload["purpose"] == "baseline_diagnostics"
        ]
        _need(len(bundles) == 1, "abandoned diagnostic proof lacks its bundle declaration")
        files = {entry["name"]: entry for entry in bundles[0].payload["files"]}
        _need(
            files["gaps.json"]["digest"] == body["gaps_digest"],
            "abandoned diagnostic proof lacks its exact gap declaration",
        )
        # Comparison preparation follows bundle publication, so its released
        # debt can also prove coverage against that committed local bundle.
        publications = [
            p
            for p in self.ledger_publications()
            if p.declaration.purpose == "baseline_diagnostics"
            and p.declaration.content_digest == bundles[0].payload["content_digest"]
        ]
        _need(len(publications) == 1, "abandoned diagnostic proof lacks its committed bundle")
        self.publication(publications[0], before=record.to_payload()["sequence"])
        raw = _read_owned(self.context.run_root / publications[0].locator / "gaps.json")
        gaps = json.loads(raw)
        _need(
            hashlib.sha256(raw).hexdigest() == body["gaps_digest"]
            and gaps["counts"] == body["counts"]
            and gaps["scheduled"] == len(body["slot_index"]) == baseline["imported"],
            "abandoned diagnostic coverage differs from committed bundle",
        )
        prefixes = body["source_prefixes"]
        _need(
            len(prefixes) == 1
            and prefixes[0]["measurement_id"] == baseline["source_measurement_id"]
            and prefixes[0]["prefix"]["content_digest"] == baseline["source_prefix_digest"],
            "abandoned diagnostic carrier differs from original baseline",
        )
        return True

    def numerical_binding(self, record: Envelope) -> Any:
        from evolution.tree.case_comparison import ComparisonProfile, build_profile

        body = (
            self.final(record)["comparison"]
            if record.event == "final_measurement_bound"
            else self.binding(record)
        )
        candidate_id = body["candidate"]["measurement_id"]
        corpus = self.context.identity_for(candidate_id).corpus_cases
        groups = {c["case_id"]: c["group_id"] for c in corpus}
        if self.context.recompute:
            rebuilt = build_profile(
                policy=self.context.policy_for(candidate_id),
                corpus=corpus,
                scopes={s["name"]: s["declared_cases"] for s in body["scopes"]},
                candidate=body["candidate"],
                reference=body["reference"],
                reference_rows=self.measurement(body["reference"]),
                epoch=body["epoch"],
                reference_kind=body["reference_kind"],
            )
            _need(
                rebuilt.to_payload() == body,
                "comparison count weights, units, policy or thresholds differ",
            )
        focus_binding_digest = None
        if "focus" in record.payload:
            before = self.context.snapshot_before(record.to_payload()["sequence"] - 1)
            attempt = before.rounds[record.payload["round"]].attempts[record.payload["attempt_id"]]
            assert attempt.family15 is not None and attempt.family15.focus_decision is not None
            focus_binding_digest = self.result(attempt.family15.focus_decision.envelope)[
                "binding_digest"
            ]
        return ComparisonProfile.from_payload(
            body,
            primary=self.context.policy_for(candidate_id).metric_primary,
            groups=groups,
            policy=self.context.policy_for(candidate_id),
            focus_binding_digest=focus_binding_digest,
        )

    def numerical_result(self, record: Envelope) -> None:
        from evolution.tree.case_comparison import (
            ComparisonResult,
            evaluate_causal_after_focus,
            evaluate_comparison,
            evaluate_focus,
            focus_rejection,
        )

        body = self.result(record)
        bound = self.campaign_record(record.payload["binding"], record.to_payload()["sequence"])
        if record.event == "global_comparison_completed":
            before = self.context.snapshot_before(record.to_payload()["sequence"] - 1)
            attempt = before.rounds[record.payload["round"]].attempts[record.payload["attempt_id"]]
            assert attempt.measurement_id is not None
            final = self.data(bound, "final")
            mids = [attempt.measurement_id, body["candidate"]["measurement_id"]]
            if final["champion_side"] == "fresh":
                mids.append(body["reference"]["measurement_id"])
            for mid in mids:
                rows = self.context.attested_rows(mid)
                if (
                    not rows
                    and mid == body["candidate"]["measurement_id"]
                    and final["verification"] == "reuse"
                ):
                    _need(
                        all(row.imported for row in self.source(body["candidate"])),
                        "unattested final Candidate contains fresh execution",
                    )
                    continue
                _need(
                    rows and self.context.attestation_verdict(mid) is None,
                    "global eligibility fails durable measurement attestation",
                )
        profile = self.numerical_binding(bound)
        if not self.context.recompute:
            return
        first = profile.policy is not None and profile.policy.focus_first
        focus_only = record.event == "focus_decided" or any(
            scope["status"] == "not_measured" for scope in body["scopes"]
        )
        candidate = self.source(body["candidate"], complete=not focus_only)
        reference = self.source(
            body["reference"],
            complete=not focus_only and profile.to_payload()["reference_kind"] == "fresh",
        )
        options: dict[str, Any] = dict(
            candidate_source=body["candidate"],
            reference_source=body["reference"],
            campaign_seed=self.context.policy_for(body["candidate"]["measurement_id"]).random_seed,
        )
        if record.event == "focus_decided":
            expected = evaluate_focus(profile, candidate, reference, **options).to_payload()
        elif first and record.event == "comparison_completed":
            before = self.context.snapshot_before(record.to_payload()["sequence"] - 1)
            attempt = before.rounds[record.payload["round"]].attempts[record.payload["attempt_id"]]
            assert attempt.family15 is not None and attempt.family15.focus_decision is not None
            focus = ComparisonResult(
                canonical_json(self.result(attempt.family15.focus_decision.envelope))
            )
            expected = (
                evaluate_causal_after_focus(profile, focus, candidate, reference, **options)
                if focus.outcome == "passed"
                else focus_rejection(profile, focus)
            ).to_payload()
        else:
            expected = evaluate_comparison(
                profile,
                self.source(body["candidate"]),
                self.source(
                    body["reference"], complete=profile.to_payload()["reference_kind"] == "fresh"
                ),
                candidate_source=body["candidate"],
                reference_source=body["reference"],
                schedules={
                    side: self.source_identity(body[side])["schedule"]
                    for side in ("candidate", "reference")
                }
                if record.event == "global_comparison_completed"
                else None,
                campaign_seed=self.context.policy_for(
                    body["candidate"]["measurement_id"]
                ).random_seed,
            ).to_payload()
        _need(
            expected == body, "comparison result differs from exact authenticated-row computation"
        )

    def grouping_descriptions(self, record: Envelope) -> None:
        """Authenticate the terminal binding directly from journal records."""
        payload = record.payload
        entries = []
        numbers = set()
        for prior in self.context.records:
            if prior.to_payload()["sequence"] >= record.to_payload()["sequence"]:
                break
            body = prior.payload
            if (
                prior.event != "grouping_full_description"
                or body["grouping_id"] != payload["grouping_id"]
            ):
                continue
            number = body["group"]
            _need(number not in numbers, "duplicate full description")
            _need(
                number in payload["result"]["assignments"],
                "full description names no assigned group",
            )
            numbers.add(number)
            entries.append(
                {"group": number, "label": body["label"], "description": body["description"]}
            )
        entries.sort(key=lambda entry: entry["group"])
        digest = hashlib.sha256(canonical_json(entries)).hexdigest() if entries else None
        _need(
            payload.get("full_descriptions_digest") == digest,
            "full descriptions differ from terminal binding",
        )

    def check(self, record: Envelope) -> None:
        if record.event == "grouping_finished":
            self.grouping_descriptions(record)
            return
        if record.event == "check_shadowed":
            return  # Closed metadata; no numerical or external evidence.
        if record.event in ("grouping_portion_finished", "grouping_batch_finished"):
            # b16 §9: a Grouper decision, checkpointed for adoption. It cites
            # no evidence; the lifecycle proves it names the started grouping,
            # and the grouping's admitted result is `grouping_finished`'s.
            return
        if record.event == "grouping_full_description":
            # b17 (ADR-0057): a copy of one model group's whole description,
            # bound by the grouping's `grouping_finished` digest. It cites no
            # evidence; the lifecycle proves it names the started grouping
            # and that one group is recorded once.
            return
        if record.event == "auxiliary_payload_declared":
            self.declaration(record)
            return
        if record.event == "comparison_bound":
            self.numerical_binding(record)
            return
        if record.event == "focus_decided":
            self.numerical_result(record)
            return
        if record.event == "gate_measurement_completed":
            p = record.payload
            bound = self.campaign_record(p["binding"], record.to_payload()["sequence"])
            _need(bound.event == "comparison_bound", "gate completion cites another binding kind")
            body = self.binding(bound)
            side = "candidate" if p["side"] == "candidate" else "reference"
            expected = self.summary(body[side])
            _need(
                {**expected, "prefix": p["source"]["prefix"]} == p["source"],
                "completed side differs from bound measurement",
            )
            if "scopes" in p:
                from evolution.tree.case_comparison import admissible

                cases = {
                    case
                    for scope in body["scopes"]
                    if scope["name"] in p["scopes"]
                    for case in scope["declared_cases"]
                }
                rows = tuple(
                    row for row in self.source(p["source"], complete=False) if row.case_id in cases
                )
                policy = self.context.policy_for(p["source"]["measurement_id"])
                _need(
                    all(admissible(row, policy.metric_primary) for row in rows),
                    "owed gate slots lack admissible rows",
                )
                name = f"measurements/{p['source']['measurement_id']}/events.jsonl"
                _need(
                    len(self.reference(p["source"]["prefix"])) == len(self.context.cache[name]),
                    "completed gate side has a later mutable tail",
                )
            else:
                rows = self.source(p["source"])
            _need(
                p["required"] == p["admissible"] == len(rows),
                "completion coverage counts differ from actual schedule",
            )
            return
        if record.event == "candidate_gate_sealed":
            self.seal(record)
            return
        if record.event == "candidate_completion_bound":
            self.completion(record)
            return
        if record.event == "candidate_completion_completed":
            bound = self.campaign_record(record.payload["binding"], record.to_payload()["sequence"])
            self.completion(bound)
            rows = self.source(record.payload["source"])
            _need(
                len(rows) == record.payload["required"] == record.payload["admissible"],
                "completion coverage count differs",
            )
            return
        if record.event == "comparison_completed":
            self.numerical_result(record)
            return
        if record.event == "global_comparison_completed":
            self.numerical_result(record)
            return
        if record.event == "final_measurement_bound":
            self.numerical_binding(record)
            return
        if record.event == "final_measurement_completed":
            p = record.payload
            bound = self.campaign_record(p["binding"], record.to_payload()["sequence"])
            if p["side"] == "candidate":
                self.final(bound)
            else:
                _need(
                    bound.event == "champion_final_bound",
                    "Champion completion cites another binding",
                )
            rows = self.source(p["source"])
            _need(len(rows) == p["required"] == p["admissible"], "final coverage count differs")
            return
        if record.event == "selection_decided":
            ranking = self.ranking(record)
            for name in ("selected_evidence", "next_champion"):
                if record.payload.get(name) is not None:
                    self.source(record.payload[name], complete=name != "next_champion")
            if not ranking["entries"]:
                _need(
                    record.payload["reason"] == "no_eligible_candidate",
                    "empty selection reason differs",
                )
            return
        if record.event == "comparison_preflight_failed":
            from evolution.tree.campaign_identity import CampaignIdentityV1, derived_identity
            from evolution.tree.campaign_policy import policy_class_for_payload
            from evolution.tree.campaign_schema import confidence_scopes
            from evolution.tree.case_comparison import build_profile, preflight
            from evolution.tree.family15_report import global_preflight

            p = record.payload
            body = self.data(record, "preflight")
            for name in ("epoch", "phase", "mode"):
                _need(body[name] == p[name], "preflight differs from campaign summary")
            prefix = self.context.snapshot_before(record.to_payload()["sequence"] - 1)
            epoch = CampaignIdentityV1.from_payload(dict(prefix.epochs[p["epoch"]]))
            raw_policy = epoch.to_payload()["policy"]
            policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
            _need(body["policy_digest"] == policy.digest, "preflight uses another epoch policy")
            state = prefix.rounds[p["round"]]
            attempt = state.attempts[p["attempt_id"]]
            reference = body["reference"]
            fresh = (
                policy.stability == "unstable"
                if p["phase"] == "early_global"
                else policy.fresh_parent
            )
            _need(
                body["reference_kind"] == ("fresh" if fresh else "stored"),
                "preflight reference knowledge differs from policy",
            )
            if p["phase"] == "early_global" and fresh:
                identity = derived_identity(epoch, prefix.champion[1], "champion")
                _need(
                    reference
                    == dict(
                        measurement_id=f"m-r{p['round']:02d}-preflight-champion",
                        identity=identity.to_payload(),
                        identity_digest=identity.digest,
                        prefix=None,
                    ),
                    "planned Champion differs from full train/condition identity",
                )
                rows = ()
            else:
                mid = attempt.parent_measurement_id if fresh else state.baseline_measurement_id
                _need(reference["measurement_id"] == mid, "preflight names another reference")
                if not fresh:
                    _need(
                        reference["prefix"] == state.baseline_reference,
                        "preflight changes baseline citation",
                    )
                rows = self.measurement(reference)
                self.reference_profile(body)
            if p["phase"] == "early_global":
                scopes = {"global": [c["case_id"] for c in epoch.corpus_cases]}
            else:
                _need(
                    attempt.selection is not None and state.gate_scope is not None,
                    "local preflight precedes exact F/C selection",
                )
                assert attempt.selection is not None and state.gate_scope is not None
                focus, causal = confidence_scopes(
                    attempt.selection, epoch.corpus_cases, state.gate_scope["cases"]
                )
                scopes = {
                    name: [epoch.corpus_cases[i]["case_id"] for i in indices]
                    for name, indices in (("focus", focus), ("causal", causal))
                }
            _need(
                [s["declared_cases"] for s in body["scopes"]] == list(scopes.values()),
                "preflight scope differs from actual frozen scope",
            )
            if self.context.recompute:
                if p["phase"] == "early_global":
                    result = global_preflight(prefix, policy, p["round"], reference, rows)
                else:
                    result = preflight(
                        build_profile(
                            policy=policy,
                            corpus=epoch.corpus_cases,
                            scopes=scopes,
                            candidate=None,
                            reference=reference,
                            reference_rows=rows,
                            epoch=p["epoch"],
                            reference_kind=body["reference_kind"],
                        ),
                        rows if not fresh else None,
                    )
                _need(
                    result.impossible and result.to_payload() == body,
                    "preflight exact ceiling, weights, policy or maximum differs",
                )
            _need(
                any(r.get("passes") is False for r in body["results"]),
                "failed preflight contains no failing scope",
            )
            return
        if record.event == "diagnostic_import_declared":
            p = record.payload
            body = self.data(record, "diagnostic_import")
            bundle = AuxiliaryPublication.from_payload(p["bundle"])
            self.publication(bundle, before=record.to_payload()["sequence"])
            raw_gaps = _read_owned(self.context.run_root / bundle.locator / "gaps.json")
            _need(
                hashlib.sha256(raw_gaps).hexdigest() == body["gaps_digest"],
                "diagnostic gap digest differs from actual bundle",
            )
            gaps = json.loads(raw_gaps)
            _need(canonical_json(gaps) == raw_gaps, "diagnostic gaps are not canonical")
            _need(
                gaps["scheduled"] == p["scheduled"] == len(body["slot_index"])
                and gaps["included"] == p["included"]
                and gaps["counts"] == body["counts"]
                and sum(body["counts"].values()) == p["scheduled"] - p["included"],
                "diagnostic coverage summary differs from actual gap declaration",
            )
            _need(
                body["destination"]["measurement_id"] == p["measurement_id"]
                and body["rows_digest"] == p["rows_digest"]
                and body["authentication"] == p["authentication"],
                "diagnostic declaration differs from external body",
            )
            self.diagnostics(record, body, bundle, gaps)
            return
        if record.event == "attempt_closed" and "path" in record.payload:
            source = self.campaign_record(
                record.payload["evidence"], record.to_payload()["sequence"]
            )
            if source.event == "comparison_completed":
                self.result(source)
            else:
                _need(
                    source.event == "comparison_preflight_failed",
                    "closure cites another evidence kind",
                )
                self.data(source, "preflight")
            return
        if record.event in {
            "attempt_opened",
            "attempt_closed",
            "finalists_bound",
            "champion_final_bound",
        }:
            # These claims are fully local: the same admitted lifecycle prefix
            # derives budget/ownership, complete finalist membership and order.
            return
        raise EvidenceMismatch(
            "family-15 event audit is not implemented", "family15_audit_incomplete"
        )

    def diagnostics(
        self,
        record: Envelope,
        body: Mapping[str, Any],
        bundle: AuxiliaryPublication,
        gaps: Mapping[str, Any],
    ) -> None:
        from evolution.tree.artifacts.artifact_payloads import _open_directory
        from evolution.tree.baseline_diagnostics import (
            _PAYLOAD_FOOTER,
            _PAYLOAD_HEADER,
            GAP_REASONS,
            MAX_SECTION_BYTES,
            _entries,
            _read_entry,
        )
        from evolution.tree.baseline_import import import_pairs_digest, record_digest
        from evolution.tree.campaign_identity import derived_identity
        from evolution.tree.execution.rollout_scoring import (
            _parse_score_result,
            parse_scorer_stdout,
        )
        from evolution.tree.storage.envelope import Envelope

        creation = self.context.records[0].payload["identity"]
        linked = creation.get("linked_from") or {}
        declared = linked.get("baseline") or {}
        rows = body["slot_index"]
        _need(
            declared.get("pairs_digest") == body["rows_digest"] == import_pairs_digest(rows)
            and declared.get("imported") == len(rows),
            "diagnostics differ from scalar admission",
        )
        prefixes = body["source_prefixes"]
        _need(
            len(prefixes) == 1
            and prefixes[0]["measurement_id"] == declared.get("source_measurement_id")
            and prefixes[0]["prefix"]["content_digest"] == declared.get("source_prefix_digest"),
            "diagnostic carrier differs from admitted linked source",
        )
        destination = body["destination"]
        from evolution.tree.campaign_identity import CampaignIdentityV1

        expected = derived_identity(
            CampaignIdentityV1.from_payload(creation),
            creation["artifact_store"]["seed_candidate_digest"],
            "champion",
        )
        _need(
            expected.to_payload() == destination["identity"],
            "diagnostic destination differs from declared baseline",
        )
        mid = destination["measurement_id"]
        if mid in self.snapshot.measurements:
            self.measurement(destination)
            actual = self.context.measurement_journal(mid)
            if actual and self.snapshot.baseline_abandoned is None:
                _need(
                    [r.payload for r in actual if r.event == "sample_imported"] == rows,
                    "diagnostic binding differs from atomic imported birth",
                )
        schedule = {(s["case_id"], s["sample_id"]): s for s in destination["identity"]["schedule"]}
        admitted = {
            (r["case_id"], schedule[r["case_id"], r["sample_id"]]["repeat"]): r for r in rows
        }
        expected_labels = dict(
            score_provenance="journal_bound",
            request="authenticated",
            stdout="captured_at_import",
            scalar="matched",
            projection="matched",
        )
        keys: list[tuple[str, int]] = []
        cursor = len(_PAYLOAD_HEADER)
        directory = self.context.run_root / bundle.locator
        for entry in _entries(directory):
            slot = entry["destination"]
            key = (slot["case_id"], slot["repeat"])
            _need(
                key in admitted and (not keys or keys[-1] < key),
                "diagnostic slots differ or repeat",
            )
            keys.append(key)
            row = admitted[key]
            _need(
                isinstance(entry["source_prefix_digest"], str)
                and re.fullmatch(r"[0-9a-f]{64}", entry["source_prefix_digest"]) is not None,
                "diagnostic original source prefix is invalid",
            )
            _need(
                slot["sample_id"] == row["sample_id"] and entry["source"] == row["source"],
                "diagnostic original provenance differs from admitted scalar row",
            )
            _need(
                entry["scalar"] == dict(metrics=row["metrics"], critical_pass=row["critical_pass"])
                and entry["validation"] == expected_labels,
                "diagnostic scalar/authentication differs",
            )
            _need(
                entry["projection"]
                == {k: destination["identity"][k] for k in ("environment", "scorer")},
                "diagnostic projection identity differs",
            )
            envelopes = {
                k: Envelope.from_payload(v, family=S1_FAMILY) for k, v in entry["records"].items()
            }
            rollout, started, finished = (
                envelopes[k] for k in ("rollout_finished", "scoring_started", "scoring_finished")
            )
            origin = row["source"]
            _need(
                record_digest(rollout) == origin["rollout_digest"]
                and record_digest(finished) == origin["scoring_digest"],
                "diagnostic source hashes differ",
            )
            _need(
                all(r.event == k for k, r in envelopes.items())
                and len({r.to_payload()["run_id"] for r in envelopes.values()}) == 1
                and started.operation_id == finished.operation_id,
                "diagnostic original record association differs",
            )
            _need(
                all(
                    r.payload["case_id"] == row["case_id"]
                    and r.payload["candidate_digest"] == origin["candidate_digest"]
                    and r.payload["sample_id"] == rollout.payload["sample_id"]
                    and r.payload.get("retry_ordinal", 0) == origin["retry_ordinal"]
                    for r in envelopes.values()
                ),
                "diagnostic original try differs",
            )
            _need(
                finished.payload["metrics"] == row["metrics"]
                and finished.payload.get("critical_pass") == row["critical_pass"],
                "diagnostic journal-bound scalar differs",
            )
            for name in ("request", "stdout"):
                section = entry["sections"][name]
                _need(
                    section["offset"] == cursor and 0 <= section["length"] <= MAX_SECTION_BYTES,
                    "diagnostic section framing or bound differs",
                )
                cursor += section["length"]
            local = _read_entry(directory, entry)
            request = json.loads(local.request)
            _need(
                isinstance(request, dict)
                and set(request) == {"scorer", "trace_id", "input", "input_sha256"}
                and isinstance(request["input"], dict)
                and set(request["input"]) == {"case_id", "public", "oracle", "view"}
                and isinstance(request["input"]["view"], dict),
                "diagnostic copied request shape differs",
            )
            _need(
                request["trace_id"]
                == started.payload["score_request_id"]
                == finished.payload["score_request_id"]
                and request["scorer"] == entry["projection"]["scorer"]["digest"]
                and started.payload["scorer"] == entry["projection"]["scorer"]
                and request["input_sha256"]
                == hashlib.sha256(canonical_json(request["input"])).hexdigest()
                and request["input"]["case_id"] == row["case_id"],
                "diagnostic copied request differs from bound scoring",
            )
            # The persisted authenticated label is the producer's capture-time
            # attestation. Independent input-digest replay additionally needs the
            # full proven corpus: tags/private are intentionally absent locally.
            if self.corpus_digest == destination["identity"]["corpus_digest"]:
                from evolution.tree.execution.journal_identity import case_payload

                case = self.corpus[row["case_id"]]
                copied = request["input"]
                _need(
                    canonical_json(copied["public"]) == canonical_json(case.public)
                    and canonical_json(copied["oracle"]) == canonical_json(case.oracle),
                    "diagnostic copied case differs from proven corpus",
                )
                # Exact journal_identity.scoring_input_digest recipe. Campaign
                # sources use tree-verifier mode (artifact_paths=[]); the frozen
                # original candidate digest replaces any filesystem reattestation.
                bound_input = dict(
                    schema_version=1,
                    case=case_payload(case),
                    view=copied["view"],
                    candidate=dict(digest=origin["candidate_digest"], artifact_paths=[]),
                    scorer=entry["projection"]["scorer"],
                )
                _need(
                    hashlib.sha256(canonical_json(bound_input)).hexdigest()
                    == started.payload["input_digest"],
                    "diagnostic copied input differs from journal-bound scoring input",
                )
            result = _parse_score_result(
                parse_scorer_stdout(local.stdout.decode()), diagnostics=True
            )
            _need(
                result.metrics == row["metrics"] and result.critical_pass == row["critical_pass"],
                "diagnostic captured stdout differs from admitted scalar",
            )
        _need(
            len(keys) == gaps["included"] == record.payload["included"],
            "diagnostic coverage differs",
        )
        _need(
            cursor + len(_PAYLOAD_FOOTER) == bundle.declaration.files[1].byte_count,
            "diagnostic payload has unindexed bytes",
        )
        with _open_directory(directory) as directory_fd:
            fd = _file(directory_fd, "payload", os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                _need(
                    stream.read(len(_PAYLOAD_HEADER)) == _PAYLOAD_HEADER,
                    "diagnostic payload header differs",
                )
                stream.seek(cursor)
                _need(
                    stream.read(len(_PAYLOAD_FOOTER) + 1) == _PAYLOAD_FOOTER,
                    "diagnostic payload footer or trailing bytes differ",
                )
        _need(
            set(gaps["counts"]) == set(GAP_REASONS) and len(gaps["samples"]) <= 64,
            "diagnostic gap bounds differ",
        )
        _need(
            set(gaps) == {"format", "scheduled", "included", "counts", "samples"}
            and gaps["format"] == "baseline-diagnostic-bundle/1",
            "diagnostic gap declaration shape differs",
        )
        unavailable = sorted(set(admitted) - set(keys))
        _need(
            len(gaps["samples"]) == min(64, len(unavailable)),
            "diagnostic bounded unavailable sample coverage differs",
        )
        sampled_counts = dict.fromkeys(GAP_REASONS, 0)
        for sample, key in zip(gaps["samples"], unavailable, strict=False):
            row = admitted[key]
            slot = dict(case_id=key[0], repeat=key[1], sample_id=row["sample_id"])
            reason = sample.get("reason")
            _need(reason in sampled_counts, "diagnostic gap sample reason is invalid")
            detailed = dict(slot=slot, reason=reason)
            expected_sample = (
                detailed
                if len(canonical_json(detailed)) <= 512
                else dict(
                    slot_sha256=hashlib.sha256(canonical_json(slot)).hexdigest(), reason=reason
                )
            )
            _need(sample == expected_sample, "diagnostic gap sample order/reference differs")
            sampled_counts[reason] += 1
        _need(
            all(sampled_counts[k] <= gaps["counts"][k] for k in GAP_REASONS),
            "diagnostic gap sample counts exceed declared counts",
        )
        request_label = (
            "authenticated" if len(keys) == len(rows) else "partial" if keys else "unavailable"
        )
        _need(
            body["authentication"]["request"] == request_label,
            "diagnostic request coverage differs",
        )

    def finding(self, record: Envelope) -> dict[str, Any] | None:
        from evolution.tree.campaign_projections import (
            _campaign_finding,
            _PreconditionFailed,
            _Unverifiable,
        )

        self.preconditions += 1
        try:
            self.check(record)
        except EvidenceMismatch as exc:
            return _campaign_finding(
                self.context, record.to_payload()["sequence"] - 1, exc.code, str(exc)
            )
        except (_PreconditionFailed, _Unverifiable):
            return _campaign_finding(
                self.context,
                record.to_payload()["sequence"] - 1,
                "family15_evidence_mismatch",
                "required campaign or measurement evidence is unavailable",
            )
        except (ArtifactError, OSError, JournalFault):
            return _campaign_finding(
                self.context,
                record.to_payload()["sequence"] - 1,
                "family15_artifact_integrity",
                "auxiliary evidence is missing, unsafe or corrupt",
            )
        except (ValueError, TypeError, KeyError, IndexError):
            return _campaign_finding(
                self.context,
                record.to_payload()["sequence"] - 1,
                "family15_payload_invalid",
                "evidence has an invalid shape",
            )
        return None


def authenticate_auxiliary(
    run_root: Path, publication: AuxiliaryPublication
) -> dict[str, Any] | None:
    """Authenticate same-run declaration, ledger terminal and canonical bytes.

    This intentionally does not claim semantic numerical verification.
    """
    from evolution.tree.campaign_projections import _evidence_of

    context, log = _evidence_of(Path(run_root))
    _need(log.journal_integrity == "complete", "campaign is not a complete admitted prefix")
    return Family15Audit(context).publication(publication)


EVENTS = frozenset(
    {
        "check_shadowed",
        "auxiliary_payload_declared",
        "attempt_opened",
        "attempt_closed",
        "comparison_preflight_failed",
        "comparison_bound",
        "comparison_completed",
        "focus_decided",
        "gate_measurement_completed",
        "candidate_gate_sealed",
        "candidate_completion_bound",
        "candidate_completion_completed",
        "final_measurement_bound",
        "finalists_bound",
        "champion_final_bound",
        "final_measurement_completed",
        "global_comparison_completed",
        "selection_decided",
        "diagnostic_import_declared",
        "grouping_portion_finished",
        "grouping_batch_finished",
        "grouping_full_description",
        "grouping_finished",
    }
)
