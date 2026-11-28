"""Authenticate standalone baseline journals and compare their measured conditions.

Sources are inspected without repairing files, acquiring a writer lease or
consulting campaign output. Their immutable facts feed the existing row admission.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evolution.tree.baseline_import import (
    BaselineSourceUnavailable,
    SourceMeasurement,
    _lease_held,
)
from evolution.tree.campaign_identity import metric_plan_digest
from evolution.tree.evaluation_journal import inspect_evaluation_journal
from evolution.tree.execution.authorizations import RefusalDifference, identity_differences
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal import inspect_journal
from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json, corpus_digest
from evolution.tree.storage.journal_errors import JournalBoundaryError


@dataclass(frozen=True)
class StandaloneSource:
    kind: Literal["evaluation", "measurement"]
    measurement: SourceMeasurement
    journal_locator: str
    configuration_digest: str | None
    split: Literal["train", "test"] | None
    row_provenance: bool


def read_standalone_source(directory: Path) -> StandaloneSource:
    """Read and authenticate a complete, unlocked standalone journal (ADR-0065)."""
    try:
        return _read(directory)
    except (OSError, JournalBoundaryError, ValueError, KeyError, TypeError):
        raise BaselineSourceUnavailable() from None


def _read(directory: Path) -> StandaloneSource:
    kind: Literal["evaluation", "measurement"]
    configuration_digest = None
    split: Literal["train", "test"] | None = None
    owner_id = None
    bound = None
    if (directory / "evaluation.jsonl").exists():
        kind = "evaluation"
        if _lease_held(directory, lock_name="evaluation.lock"):
            raise BaselineSourceUnavailable()
        owner = inspect_evaluation_journal(directory)
        if not owner.records or owner.journal_integrity != "complete":
            raise BaselineSourceUnavailable()
        first = owner.records[0]
        configuration = first.payload["identity"]
        if configuration.get("profile") != "evaluation/1" or configuration.get("split") not in (
            "train",
            "test",
        ):
            raise BaselineSourceUnavailable()
        configuration_digest = first.payload["identity_digest"]
        split = configuration["split"]
        owner_id = first.to_payload()["run_id"]
        bound = next(
            (record.payload for record in owner.records if record.event == "measurement_bound"),
            None,
        )
        if bound is None:
            raise BaselineSourceUnavailable()
        locator = "measurements/evaluation/events.jsonl"
        child = directory / "measurements" / "evaluation"
    elif (directory / "events.jsonl").exists():
        kind = "measurement"
        locator = "events.jsonl"
        child = directory
    else:
        raise BaselineSourceUnavailable()
    if _lease_held(child):
        raise BaselineSourceUnavailable()
    snapshot = inspect_journal(child)
    if not snapshot.records or snapshot.journal_integrity != "complete":
        raise BaselineSourceUnavailable()
    first = snapshot.records[0]
    created = first.payload
    identity = created["identity"]
    if kind == "evaluation":
        if identity.get("profile") != "rollout_evaluation/1" or bound != {
            "identity": identity,
            "identity_digest": created["identity_digest"],
        }:
            raise BaselineSourceUnavailable()
    elif identity.get("profile") != "rollout_measurement" or "connection_binding" in identity:
        raise BaselineSourceUnavailable()
    if any(record.event == "sample_imported" for record in snapshot.records):
        raise BaselineSourceUnavailable()
    run_id = owner_id if owner_id is not None else first.to_payload()["run_id"]
    measurement = SourceMeasurement(
        run_id=run_id,
        measurement_id="evaluation" if kind == "evaluation" else run_id,
        identity_digest=created["identity_digest"],
        prefix_digest=snapshot.prefix_digest,
        records=snapshot.records,
        valid_bytes=snapshot.valid_bytes,
        journal_integrity=snapshot.journal_integrity,
    )
    return StandaloneSource(
        kind, measurement, locator, configuration_digest, split, identity.get("row_provenance") == 1
    )


def held_out_finding(
    source: StandaloneSource, partition: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Prove train separation against the campaign's own bound partition."""
    identity = source.measurement.records[0].payload["identity"]
    scheduled = {entry["case_id"] for entry in identity["schedule"]}
    held = {case_id for case_id, split in partition["assignments"] if split == "test"}
    intersection = sorted(scheduled & held)
    if intersection or (source.kind == "evaluation" and source.split != "train"):
        return {"split": source.split, "cases": intersection}
    return None


def authorizable_differences(
    source: StandaloneSource, *, candidate_digest: str, scorer: Mapping[str, str], primary: str
) -> tuple[RefusalDifference, ...]:
    """Compare the three local fields before starting Target preflight."""
    before = source.measurement.records[0].payload["identity"]
    observed = dict(
        candidate_digest=candidate_digest,
        scorer=dict(scorer),
        metric_plan_digest=metric_plan_digest(primary),
    )
    previous = {name: before[name] for name in observed}
    if (
        before["metric_plan_digest"]
        == hashlib.sha256(canonical_json({"primary": primary})).hexdigest()
    ):
        previous["metric_plan_digest"] = observed["metric_plan_digest"]
    return identity_differences({"identity": previous}, {"identity": observed})


def caveat_fields(
    source: StandaloneSource,
    target: RunIdentityV1,
    partition: Mapping[str, Any],
    *,
    corpus: Sequence[EvalCase] | None = None,
) -> tuple[str, ...]:
    """Compare measured conditions; operational limits never change comparability.

    Subset content needs the train cases as well as the partition descriptor,
    which intentionally holds only assignments and digests.
    """
    before = source.measurement.records[0].payload["identity"]
    after = target.to_payload()
    names = ("target", "environment", "budget", "materialization_strategy")
    fields = {
        difference.field
        for difference in identity_differences(
            {"identity": {name: before[name] for name in names}},
            {"identity": {name: after[name] for name in names}},
        )
    }
    scheduled = {entry["case_id"] for entry in before["schedule"]}
    train = {case_id for case_id, split in partition["assignments"] if split == "train"}
    if scheduled != train:
        fields.add("identity.corpus_cases")
    if not scheduled <= train:
        fields.add("identity.corpus_digest")
    else:
        if corpus is None:
            if scheduled != train:
                raise ValueError("train corpus required for a subset comparison")
            expected_digest = after["corpus_digest"]
        else:
            expected_digest = corpus_digest([case for case in corpus if case.case_id in scheduled])
        if before["corpus_digest"] != expected_digest:
            fields.add("identity.corpus_digest")

    def schedule_keys(identity: Mapping[str, Any]) -> set[tuple[str, int, int | None]]:
        return {
            (row["case_id"], row["repeat"], row["requested_seed"]) for row in identity["schedule"]
        }

    if schedule_keys(before) != schedule_keys(after):
        fields.add("identity.schedule")
    return tuple(sorted(fields))


@dataclass(frozen=True)
class ImportFinding:
    code: str
    instance_digest: str
    differences: tuple[RefusalDifference, ...] = ()
    effect: Literal["import"] = "import"


class BaselineImportRefused(Exception):
    """The source is readable, but one or more separate decisions are missing."""

    def __init__(self, findings: tuple[ImportFinding, ...]) -> None:
        self.findings = findings
        super().__init__("baseline_import_refused")
