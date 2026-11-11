"""Family-15 pre-Promotion transfer from an own, complete sealed gate."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from evolution.tree.baseline_import import (
    BaselineImport,
    FrozenSourceCorrupt,
    SourceMeasurement,
    admit_rows,
)
from evolution.tree.execution.journal_identity import RunIdentityV1
from evolution.tree.family15_schema import validate_comparison_data


@dataclass(frozen=True)
class MeasurementBinding:
    """Immutable destination identity, including schedule and projection digests."""

    measurement_id: str
    identity: RunIdentityV1
    primary: str
    condition_fields: tuple[str, ...] = ()


def validate_candidate_gate(source: SourceMeasurement, gate_seal: Mapping[str, Any]) -> None:
    """Check reader-owned physical prefix metadata before any source recovery."""
    try:
        validate_comparison_data(dict(gate_seal))
        if gate_seal["kind"] != "gate_seal":
            raise ValueError("not a gate seal")
        sealed = gate_seal["measurement"]
        prefix = sealed["prefix"]
        if (
            sealed["measurement_id"] != source.measurement_id
            or sealed["identity_digest"] != source.identity_digest
            or source.records[0].payload["identity_digest"] != source.identity_digest
            or sealed["identity"] != source.records[0].payload["identity"]
            or prefix["content_digest"] != source.prefix_digest
            or source.journal_integrity != "complete"
            or source.valid_bytes is None
            or prefix["locator"]
            != f"measurements/{source.measurement_id}/events.jsonl#bytes=0:{source.valid_bytes}"
            or prefix["run_id"] != source.records[0].to_payload()["run_id"]
        ):
            raise ValueError("sealed source differs")
    except (ValueError, KeyError, IndexError, TypeError):
        raise FrozenSourceCorrupt(source.measurement_id, "candidate_gate_seal_mismatch") from None


def prepare_candidate_completion(
    source: SourceMeasurement,
    destination: MeasurementBinding,
    *,
    gate_seal: Mapping[str, Any],
) -> BaselineImport:
    """Authenticate the complete seal, then use ADR-0017's sole row admission rule.

    The caller authenticates the seal's campaign authority and reads the frozen
    file without recovery. No rejected source can fall back to fresh dispatch.
    """
    validate_candidate_gate(source, gate_seal)
    try:
        sealed = gate_seal["measurement"]
        if destination.measurement_id == source.measurement_id:
            raise ValueError("completion needs a new destination")
        old, new = sealed["identity"], destination.identity.to_payload()
        for field in (
            "candidate_digest",
            "scorer",
            "corpus_digest",
            "metric_plan_digest",
            "evaluation_set_id",
            "row_provenance",
        ):
            if old.get(field) != new.get(field):
                raise ValueError(f"completion {field} differs")
        original = admit_rows(
            source,
            RunIdentityV1.from_payload(old),
            primary=destination.primary,
            condition_changed=(),
            primary_range=True,
        )
        if original.imported != len(old["schedule"]):
            raise ValueError("sealed gate is not fully admissible under its original identity")
        slots = {(s["case_id"], s["repeat"]): s for s in new["schedule"]}
        # b18: a gate measured with `case_repeats` (`/16` focus repeats) has
        # focus repeats past the completion's R; only those may stay behind.
        horizon: dict[str, int] = {}
        for s in new["schedule"]:
            horizon[s["case_id"]] = max(horizon.get(s["case_id"], 0), int(s["repeat"]))
        extended = "case_repeats" in old
        for slot in old["schedule"]:
            target = slots.get((slot["case_id"], slot["repeat"]))
            if target is None and not (
                extended and int(slot["repeat"]) > horizon.get(slot["case_id"], slot["repeat"])
            ):
                raise ValueError("completion omits a gate slot")
        # An authorized condition change may legitimately omit formerly valid
        # rows. Destination non-admission is owed work, not source corruption.
        return admit_rows(
            source,
            destination.identity,
            primary=destination.primary,
            condition_changed=destination.condition_fields,
            primary_range=True,
        )
    except (ValueError, KeyError, IndexError, TypeError):
        raise FrozenSourceCorrupt(source.measurement_id, "candidate_gate_seal_mismatch") from None
