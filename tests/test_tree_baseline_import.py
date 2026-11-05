"""Task 4: the admission rule (spec §3.2) and the source measurement reader (§5.0, §5.1)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.tree.baseline_import import (
    BaselineSourceUnavailable,
    FrozenSourceCorrupt,
    SourceMeasurement,
    admit_rows,
    fields_between,
    import_pairs_digest,
    import_sealed,
    read_frozen_source,
    read_source,
)
from evolution.tree.campaign_identity import CampaignIdentityV1, measurement_identity
from evolution.tree.campaign_measurement import Measurement, stage_plans
from evolution.tree.execution.journal_identity import RunIdentityV1
from evolution.tree.execution.journal_schema import S1_FAMILY, components_digest
from evolution.tree.execution.sample_outcomes import disposition_for
from evolution.tree.storage.envelope import Envelope
from tests.campaign_fakes import PRIMARY
from tests.test_tree_campaign_measurement import (
    CAMPAIGN_RUN_ID,
    MEASUREMENT_ID,
    interrupted_measurement,
    sealed_subset_measurement,
    tear_last_record,
    two_looks,
)
from tests.tree.execution.test_journal_schema import IMPORT_SOURCE

TS = "2026-09-18T12:00:00Z"
DEP_DIGEST = "d" * 64
CANDIDATE_DIGEST = "cand-1"
SCORER = dict(kind="judge", digest="scorer", recovery="non_replayable")
DEFAULT_METRICS = {"score": 1.0}


def _envelope(seq: int, event: str, payload: dict[str, Any], *, op: str | None = None) -> Envelope:
    return Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="measurement-run",
            sequence=seq,
            timestamp=TS,
            event=event,
            operation_id=op,
            payload=payload,
        ),
        family=S1_FAMILY,
    )


def _schedule_rows(
    pairs: list[tuple[str, str]], seed_mode: str, seed: int, side: str
) -> list[dict[str, Any]]:
    """Schedule entries as `campaign_identity.schedule_for_ids` builds them.

    `pairs` name `(case_id, repeat label)`; under `exact` the sample id is the
    bare label, under `unpaired`/`none` it carries the measurement's `side`
    (`candidate-01`, `champion-01`), exactly like the real schedule builder.
    """

    rows = []
    for case_id, label in pairs:
        repeat = int(label)
        requested_seed = seed + repeat if seed_mode == "exact" else None
        sample_id = label if seed_mode == "exact" else f"{side}-{label}"
        rows.append(
            dict(case_id=case_id, sample_id=sample_id, repeat=repeat, requested_seed=requested_seed)
        )
    return rows


def _identity_payload(
    *, candidate_digest: str, seed_mode: str, dependency_digest: str, schedule: list[dict[str, Any]]
) -> dict[str, Any]:
    return dict(
        profile="rollout_measurement",
        candidate_digest=candidate_digest,
        target=dict(
            contract_digest="contract",
            dependency_digest=dependency_digest,
            settings_hash="settings",
            seed_mode=seed_mode,
            protocol_version="1",
            runtime_ref_digest="runtime",
        ),
        environment=dict(kind="env", digest="envdigest"),
        scorer=dict(SCORER),
        evaluation_set_id="label",
        corpus_digest="a" * 64,
        metric_plan_digest="b" * 64,
        schedule=schedule,
        budget=dict(max_turns=2, timeout_seconds=1.5),
        max_concurrency=1,
        max_scorer_concurrency=1,
        materialization_strategy="filesystem_overlay",
    )


def source_measurement(
    *,
    rows: dict[tuple[str, str], str] | None = None,
    metrics: dict[str, float] | None = None,
    seed: int = 1,
    seed_mode: str = "exact",
    dependency_digest: str = DEP_DIGEST,
    candidate_digest: str = CANDIDATE_DIGEST,
    imported_from: str | None = None,
    imported_fields: list[str] | None = None,
    campaign_run_id: str = "pred-1",
    measurement_id: str = "m-r00-champion",
    side: str = "candidate",
    row_provenance: bool = False,
    provenance: list[str] | None = None,
    provenance_overflow: bool = False,
    components: dict[str, Any] | None = None,
) -> SourceMeasurement:
    """A hand-built, `Lifecycle`-valid predecessor measurement.

    `rows` maps `(case_id, repeat label)` to an outcome tag: `"score"` builds a
    scored `completed` try (with `metrics`), any other string is the literal
    `rollout_finished` outcome of an unscored try (e.g. `"time_budget"`).
    `imported_from` builds a single carried (`sample_imported`) row instead,
    chaining from a grandparent run, with `imported_fields` as its stored
    `source.condition_fields`. `side` is the measurement side whose sample ids
    the schedule carries under `unpaired`/`none` (default `candidate`: a
    promoted predecessor's champion measurement is its winner's attempt).

    `row_provenance` marks the journal family 12 (G2b1); every row then
    carries `provenance` (or `provenance_overflow`) and, with `components`,
    the digest of that map, which the journal declares before its first
    rollout.
    """

    if imported_from is not None:
        rows = {("c", "01"): "imported"}
    elif rows is None:
        rows = {("c", "01"): "score"}
    resolved_metrics = dict(metrics) if metrics is not None else dict(DEFAULT_METRICS)

    case_ids = sorted({case_id for case_id, _ in rows})
    max_repeat = max(int(sample_id) for _, sample_id in rows)
    pairs = [(case_id, f"{r:02d}") for case_id in case_ids for r in range(1, max_repeat + 1)]
    schedule = _schedule_rows(pairs, seed_mode, seed, side)
    schedule_by_key = {(row["case_id"], f"{row['repeat']:02d}"): row for row in schedule}

    identity_payload = _identity_payload(
        candidate_digest=candidate_digest,
        seed_mode=seed_mode,
        dependency_digest=dependency_digest,
        schedule=schedule,
    )
    if row_provenance:
        identity_payload["row_provenance"] = 1
    identity = RunIdentityV1.from_payload(identity_payload)
    row_fields: dict[str, Any] = {}
    if provenance is not None:
        row_fields["provenance"] = sorted(set(provenance))
    if provenance_overflow:
        row_fields["provenance_overflow"] = True
    if components is not None:
        row_fields["components_digest"] = components_digest(components)

    records = [
        _envelope(
            1,
            "run_created",
            dict(
                identity=identity.to_payload(),
                identity_digest=identity.digest,
                producing_version="0.2.0rc2",
            ),
        )
    ]
    seq = 2
    op_counter = 0
    for key in pairs:
        tag = rows.get(key)
        if tag is None:
            continue
        entry = schedule_by_key[key]
        case_id, sample_id = entry["case_id"], entry["sample_id"]
        if tag == "imported":
            payload = dict(
                case_id=case_id,
                sample_id=sample_id,
                candidate_digest=candidate_digest,
                outcome="completed",
                metrics=dict(resolved_metrics),
                critical_pass=None,
                effective_seed=entry["requested_seed"],
                dependency_digest=dependency_digest,
                source=dict(
                    run_id=imported_from,
                    measurement_id="m-r00-champion",
                    identity_digest="e" * 64,
                    candidate_digest="f" * 64,
                    retry_ordinal=0,
                    rollout_digest="1" * 64,
                    scoring_digest="2" * 64,
                    condition_fields=sorted(set(imported_fields or [])),
                ),
            )
            payload.update({k: v for k, v in row_fields.items() if k != "provenance_overflow"})
            records.append(_envelope(seq, "sample_imported", payload))
            seq += 1
            continue

        if components is not None and not any(r.event == "components_declared" for r in records):
            records.append(
                _envelope(
                    seq,
                    "components_declared",
                    dict(components_digest=components_digest(components), components=components),
                )
            )
            seq += 1
        op_counter += 1
        rollout_op = f"rollout-{op_counter}"
        records.append(
            _envelope(
                seq,
                "rollout_started",
                dict(case_id=case_id, sample_id=sample_id, candidate_digest=candidate_digest),
                op=rollout_op,
            )
        )
        seq += 1
        outcome = "completed" if tag == "score" else tag
        records.append(
            _envelope(
                seq,
                "rollout_finished",
                dict(
                    case_id=case_id,
                    sample_id=sample_id,
                    candidate_digest=candidate_digest,
                    outcome=outcome,
                    **row_fields,
                ),
                op=rollout_op,
            )
        )
        seq += 1
        if disposition_for(outcome).kind == "score":
            op_counter += 1
            score_op = f"score-{op_counter}"
            records.append(
                _envelope(
                    seq,
                    "scoring_started",
                    dict(
                        case_id=case_id,
                        sample_id=sample_id,
                        candidate_digest=candidate_digest,
                        score_request_id=score_op,
                        input_schema_version=1,
                        input_digest="a" * 64,
                        scorer=dict(SCORER),
                    ),
                    op=score_op,
                )
            )
            seq += 1
            records.append(
                _envelope(
                    seq,
                    "scoring_finished",
                    dict(
                        case_id=case_id,
                        sample_id=sample_id,
                        candidate_digest=candidate_digest,
                        score_request_id=score_op,
                        metrics=dict(resolved_metrics),
                        critical_pass=None,
                    ),
                    op=score_op,
                )
            )
            seq += 1

    return SourceMeasurement(
        run_id=campaign_run_id,
        measurement_id=measurement_id,
        identity_digest=identity.digest,
        prefix_digest="prefix-digest",
        records=tuple(records),
    )


def target_identity(
    *,
    cases: list[str] | None = None,
    seed: int = 1,
    seed_mode: str = "exact",
    dependency_digest: str = DEP_DIGEST,
    candidate_digest: str = CANDIDATE_DIGEST,
    row_provenance: bool = False,
) -> RunIdentityV1:
    pairs = [(case_id, "01") for case_id in (cases or ["c"])]
    schedule = _schedule_rows(pairs, seed_mode, seed, "champion")
    payload = _identity_payload(
        candidate_digest=candidate_digest,
        seed_mode=seed_mode,
        dependency_digest=dependency_digest,
        schedule=schedule,
    )
    if row_provenance:
        payload["row_provenance"] = 1
    return RunIdentityV1.from_payload(payload)


def snapshot_naming(measurement_id: str, *, run_id: str = "pred-1") -> Any:
    """A duck-typed `CampaignSnapshot` stand-in: only `champion_measurement` and `run_id` matter here."""

    return SimpleNamespace(run_id=run_id, rounds={}, measurements={measurement_id: {}})


def test_fields_between_accumulates_the_changes_from_the_source_epoch_to_the_target():
    """G2b4: a completion's carried rows name every field changed since its source."""

    snapshot = SimpleNamespace(
        condition_changes=({"fields": ["b", "a"]}, {"fields": ["c", "a"]}),
        epochs=({}, {}, {}),
        measurements={
            "source": {"epoch": 0},
            "same": {"epoch": 0},
            "next": {"epoch": 1},
            "latest": {"epoch": 2},
        },
    )
    assert fields_between(snapshot, "source", "same") == ()
    assert fields_between(snapshot, "source", "next") == ("a", "b")
    assert fields_between(snapshot, "source", "latest") == ("a", "b", "c")
    # A target bound now is in the latest epoch.
    assert fields_between(snapshot, "source") == ("a", "b", "c")
    assert fields_between(snapshot, "next") == ("a", "c")


def test_admission_takes_scored_scheduled_rows_with_the_primary_metric():
    source = source_measurement(
        rows={("c1", "01"): "score", ("c1", "02"): "time_budget", ("c2", "01"): "score"},
        metrics={"score": 1.0},
    )
    target = target_identity(cases=["c1", "c2", "c3"], seed_mode="exact")
    result = admit_rows(
        source, target, primary="score", condition_changed=["identity.scorer.digest"]
    )
    assert [(r["case_id"], r["sample_id"]) for r in result.rows] == [("c1", "01"), ("c2", "01")]
    assert result.scheduled == 3 and result.imported == 2
    assert result.fields == ("identity.scorer.digest",) and result.origin_runs == 1
    assert result.pairs_digest == import_pairs_digest(result.rows)


def test_a_changed_random_seed_admits_nothing_under_exact():
    result = admit_rows(
        source_measurement(seed=1),
        target_identity(seed=2, seed_mode="exact"),
        primary="score",
        condition_changed=["identity.policy_digest"],
    )
    assert result.imported == 0


def test_exact_needs_the_targets_dependency_digest_unpaired_does_not():
    exact_source = source_measurement(dependency_digest="a" * 64, seed_mode="exact")
    exact = admit_rows(
        exact_source,
        target_identity(dependency_digest="b" * 64, seed_mode="exact"),
        primary="score",
        condition_changed=[],
    )
    assert exact.imported == 0

    unpaired_source = source_measurement(dependency_digest="a" * 64, seed_mode="unpaired")
    unpaired = admit_rows(
        unpaired_source,
        target_identity(dependency_digest="b" * 64, seed_mode="unpaired"),
        primary="score",
        condition_changed=[],
    )
    assert unpaired.imported > 0


@pytest.mark.parametrize("seed_mode", ["unpaired", "none"])
@pytest.mark.parametrize("source_side", ["candidate", "champion"])
def test_an_unpaired_row_matches_by_repeat_and_takes_the_targets_sample_id(seed_mode, source_side):
    """Ruling R8: under `unpaired`/`none` sample ids carry the measurement side, so a
    promoted predecessor's winner (`candidate-01`) must still reach the successor's
    `champion-01`. Rule 1 keys on `(case_id, repeat, requested_seed)` and the admitted
    payload carries the target schedule entry's own `sample_id`."""

    source = source_measurement(
        rows={("c1", "01"): "score", ("c1", "02"): "score", ("c2", "01"): "score"},
        seed_mode=seed_mode,
        side=source_side,
    )
    target = target_identity(cases=["c1", "c2"], seed_mode=seed_mode)
    result = admit_rows(source, target, primary="score", condition_changed=[])
    assert [(r["case_id"], r["sample_id"]) for r in result.rows] == [
        ("c1", "champion-01"),
        ("c2", "champion-01"),
    ]
    assert result.imported == 2 and result.scheduled == 2
    assert result.pairs_digest == import_pairs_digest(result.rows)


def test_a_carried_unpaired_row_matches_by_repeat_too():
    source = source_measurement(
        imported_from="grand-1", imported_fields=["x"], seed_mode="unpaired", side="champion"
    )
    result = admit_rows(
        source, target_identity(seed_mode="unpaired"), primary="score", condition_changed=["y"]
    )
    assert [(r["sample_id"], r["source"]["run_id"]) for r in result.rows] == [
        ("champion-01", "grand-1")
    ]


def test_an_exact_source_row_is_not_admitted_by_an_unpaired_target():
    """Ruling R4: rule 1 is a literal triple match; an exact source's real seed never equals
    an unpaired target's `None`, so nothing is admitted regardless of any other field."""

    result = admit_rows(
        source_measurement(seed_mode="exact"),
        target_identity(seed_mode="unpaired"),
        primary="score",
        condition_changed=[],
    )
    assert result.imported == 0


def test_a_missing_primary_metric_is_measured_again():
    result = admit_rows(
        source_measurement(metrics={"other": 1.0}),
        target_identity(),
        primary="score",
        condition_changed=[],
    )
    assert result.imported == 0


def test_a_carried_row_keeps_its_origin_and_accumulates_fields():
    source = source_measurement(imported_from="grand-1", imported_fields=["x"])
    result = admit_rows(source, target_identity(), primary="score", condition_changed=["y"])
    assert result.imported == 1
    row = result.rows[0]
    assert row["source"]["run_id"] == "grand-1"
    assert row["source"]["condition_fields"] == ["x", "y"]
    assert result.fields == ("x", "y")


def test_an_unreadable_source_is_baseline_source_unavailable(tmp_path):
    with pytest.raises(BaselineSourceUnavailable):
        read_source(tmp_path / "absent", snapshot_naming("m-r00-champion"))


def test_a_scored_candidate_failed_row_is_carried():
    """PDR-0013 and ADR-0017 admit a row because it is *scored*, never by a list
    of terminals. `candidate_failed` is a scoring terminal owned by the
    Candidate (W19), so its row is genuine quality data and is carried."""

    source = source_measurement(
        rows={("c1", "01"): "candidate_failed", ("c2", "01"): "score"},
        metrics={"score": 0.0},
    )
    target = target_identity(cases=["c1", "c2"], seed_mode="exact")
    result = admit_rows(source, target, primary="score", condition_changed=[])
    assert [(r["case_id"], r["outcome"]) for r in result.rows] == [
        ("c1", "candidate_failed"),
        ("c2", "completed"),
    ]
    assert result.imported == 2


# -- in-run carried rows (I2 spec §5.3) --------------------------------------


def _new_epoch_target(created: dict[str, Any]) -> RunIdentityV1:
    """The frozen measurement's identity with a changed agent-side `target` field."""

    payload = dict(created["identity"])
    payload["target"] = dict(payload["target"], settings_hash="settings-after-the-change")
    return RunIdentityV1.from_payload(payload)


async def test_a_frozen_measurement_carries_exactly_its_scored_latest_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await interrupted_measurement(tmp_path, monkeypatch)
    created = Measurement.open_frozen(rig.run_root, MEASUREMENT_ID).records()[0].payload

    source = read_frozen_source(
        rig.run_root,
        CAMPAIGN_RUN_ID,
        MEASUREMENT_ID,
        expected_identity_digest=created["identity_digest"],
    )
    raw = (rig.run_root / "measurements" / MEASUREMENT_ID / "events.jsonl").read_bytes()
    assert isinstance(source, SourceMeasurement)
    assert source.run_id == CAMPAIGN_RUN_ID
    assert source.measurement_id == MEASUREMENT_ID
    assert source.identity_digest == created["identity_digest"]
    assert source.prefix_digest == hashlib.sha256(raw).hexdigest()

    admitted = admit_rows(
        source, _new_epoch_target(created), primary=PRIMARY, condition_changed=["target"]
    )

    by_pair = {(row["case_id"], row["sample_id"]): row for row in admitted.rows}
    # Scored latest tries and the already-carried pair; never the indeterminate
    # `case-01/02` or the in-flight `case-01/01`.
    assert sorted(by_pair) == [
        ("case-01", "03"),
        ("case-02", "01"),
        ("case-02", "02"),
        ("case-02", "03"),
    ]
    assert admitted.imported == 4 and admitted.scheduled == 6
    for sample in ("01", "02", "03"):
        carried = by_pair[("case-02", sample)]["source"]
        assert carried["run_id"] == CAMPAIGN_RUN_ID
        assert carried["measurement_id"] == MEASUREMENT_ID
        assert carried["identity_digest"] == created["identity_digest"]
        assert carried["condition_fields"] == ["target"]
    # A row that was itself imported keeps its origin and accumulates the field.
    transitive = by_pair[("case-01", "03")]["source"]
    assert transitive == dict(
        IMPORT_SOURCE,
        condition_fields=sorted({*IMPORT_SOURCE["condition_fields"], "target"}),
    )
    assert admitted.fields == tuple(sorted({*IMPORT_SOURCE["condition_fields"], "target"}))
    assert admitted.origin_runs == 2
    assert admitted.source_prefix_digest == source.prefix_digest
    assert admitted.pairs_digest == import_pairs_digest(admitted.rows)
    # Reading twice is deterministic: the frozen source cannot move.
    again = read_frozen_source(
        rig.run_root,
        CAMPAIGN_RUN_ID,
        MEASUREMENT_ID,
        expected_identity_digest=created["identity_digest"],
    )
    assert again == source


async def test_a_frozen_source_bound_under_another_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await interrupted_measurement(tmp_path, monkeypatch, carried=False)

    with pytest.raises(FrozenSourceCorrupt, match=MEASUREMENT_ID) as refused:
        read_frozen_source(
            rig.run_root, CAMPAIGN_RUN_ID, MEASUREMENT_ID, expected_identity_digest="e" * 64
        )
    assert refused.value.measurement_id == MEASUREMENT_ID
    assert refused.value.reason == "identity_digest_mismatch"


def test_an_absent_frozen_source_is_refused_not_unavailable(tmp_path: Path) -> None:
    with pytest.raises(FrozenSourceCorrupt) as refused:
        read_frozen_source(tmp_path, "run", "m-r01-a01", expected_identity_digest="e" * 64)
    assert refused.value.reason == "unreadable"
    assert not isinstance(refused.value, BaselineSourceUnavailable)


async def test_a_torn_frozen_source_carries_the_same_rows_on_every_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owed-copy recovery re-reads the source: a torn tail must not move its digest."""

    rig = await interrupted_measurement(tmp_path, monkeypatch)
    created = Measurement.open_frozen(rig.run_root, MEASUREMENT_ID).records()[0].payload
    _, torn = tear_last_record(rig)

    def read() -> Any:
        source = read_frozen_source(
            rig.run_root,
            CAMPAIGN_RUN_ID,
            MEASUREMENT_ID,
            expected_identity_digest=created["identity_digest"],
        )
        return source, admit_rows(
            source, _new_epoch_target(created), primary=PRIMARY, condition_changed=["target"]
        )

    (first_source, first), (second_source, second) = read(), read()

    assert first_source.prefix_digest == second_source.prefix_digest
    assert first.rows == second.rows and first.rows
    assert first.pairs_digest == second.pairs_digest
    path = rig.run_root / "measurements" / MEASUREMENT_ID / "events.jsonl"
    assert path.read_bytes() == torn


# -- sealed same-run source (G2 spec §3.2, ADR-0017 amended 2026-09-30) ------


def _completion_target(rig: Any, candidate_digest: str | None = None) -> RunIdentityV1:
    from tests.test_tree_campaign_measurement import measurement_identity as shared_identity

    return shared_identity(rig.identity, rig.cases, candidate_digest or rig.ref.digest, "champion")


async def test_a_sealed_subset_winner_completes_into_a_full_corpus_measurement(
    tmp_path: Path,
) -> None:
    rig, winner = await sealed_subset_measurement(tmp_path)
    target = _completion_target(rig)

    admitted = import_sealed(
        rig.run_root,
        CAMPAIGN_RUN_ID,
        winner.measurement_id,
        target,
        expected_identity_digest=winner.identity.digest,
        primary=PRIMARY,
    )

    assert {(row["case_id"], row["sample_id"]) for row in admitted.rows} == {
        (case_id, sample) for case_id in ("case-01", "case-03") for sample in ("01", "02", "03")
    }
    assert admitted.imported == 6 and admitted.scheduled == 9
    assert admitted.fields == () and admitted.origin_runs == 1
    for row in admitted.rows:
        assert row["candidate_digest"] == rig.ref.digest
        assert row["source"]["run_id"] == CAMPAIGN_RUN_ID
        assert row["source"]["measurement_id"] == winner.measurement_id
        assert row["source"]["candidate_digest"] == rig.ref.digest
        assert row["source"]["condition_fields"] == []

    # Declare, then atomic birth: the completion measures only the unselected case.
    completion = Measurement.declare(
        run_root=rig.run_root,
        campaign_run_id=CAMPAIGN_RUN_ID,
        measurement_id="m-r01-completion",
        identity=target,
        corpus=rig.cases,
    )
    completion.create(imported=admitted.rows)
    try:
        # The fake adapter records sessions under the materialized ref, which is
        # the rig's champion ref, so the completion's sessions land in the
        # champion's order; compare the delta.
        before = rig.adapter.session_case_order(measurement=MEASUREMENT_ID)
        for plan in stage_plans(
            rig.policy, completion.scheduled_cases, rig.adapter.seed_mode, "champion"
        ):
            await completion.run_stage(plan, **rig.stage_kwargs())
        assert completion.complete()
        after = rig.adapter.session_case_order(measurement=MEASUREMENT_ID)
        assert set(after[len(before) :]) == {"case-02"}
        assert sum(row.imported for row in completion.sample_rows(3)) == 6
    finally:
        completion.close()


async def test_a_winner_promoted_at_an_early_look_carries_only_the_repeats_it_holds(
    tmp_path: Path,
) -> None:
    rig, winner = await sealed_subset_measurement(tmp_path, campaign_policy=two_looks(), stages=1)
    admitted = import_sealed(
        rig.run_root,
        CAMPAIGN_RUN_ID,
        winner.measurement_id,
        _completion_target(rig),
        expected_identity_digest=winner.identity.digest,
        primary=PRIMARY,
    )
    assert {row["sample_id"] for row in admitted.rows} == {"01", "02", "03"}
    assert admitted.imported == 6 and admitted.scheduled == 18


async def test_a_sealed_source_of_another_tree_is_refused(tmp_path: Path) -> None:
    rig, winner = await sealed_subset_measurement(tmp_path)
    with pytest.raises(FrozenSourceCorrupt) as refused:
        import_sealed(
            rig.run_root,
            CAMPAIGN_RUN_ID,
            winner.measurement_id,
            _completion_target(rig, "other-tree"),
            expected_identity_digest=winner.identity.digest,
            primary=PRIMARY,
        )
    assert refused.value.reason == "candidate_digest_mismatch"
    assert refused.value.measurement_id == winner.measurement_id


async def test_an_owed_sealed_copy_that_does_not_reproduce_its_declaration_is_corruption(
    tmp_path: Path,
) -> None:
    rig, winner = await sealed_subset_measurement(tmp_path)
    target = _completion_target(rig)

    def owed(declared: tuple[int, str] | None) -> Any:
        return import_sealed(
            rig.run_root,
            CAMPAIGN_RUN_ID,
            winner.measurement_id,
            target,
            expected_identity_digest=winner.identity.digest,
            primary=PRIMARY,
            declared=declared,
        )

    declaration = owed(None)
    assert owed((declaration.imported, declaration.pairs_digest)) == declaration

    source = rig.run_root / "measurements" / winner.measurement_id / "events.jsonl"
    lines = source.read_bytes().splitlines(keepends=True)
    last_scored = max(i for i, line in enumerate(lines) if b'"scoring_finished"' in line)
    source.write_bytes(b"".join(lines[:last_scored]))  # a scored row is gone

    with pytest.raises(FrozenSourceCorrupt) as refused:
        owed((declaration.imported, declaration.pairs_digest))
    assert refused.value.reason == "pairs_digest_mismatch"
    assert not isinstance(refused.value, BaselineSourceUnavailable)


async def test_a_sealed_source_across_an_accepted_change_names_the_changed_fields(
    tmp_path: Path,
) -> None:
    """G2 spec §4.7: a completion bound after `continue` imports across the epoch."""

    rig, winner = await sealed_subset_measurement(tmp_path)
    payload = rig.identity.to_payload()
    payload["target"]["settings_hash"] = "settings-after-the-change"
    changed = CampaignIdentityV1.from_payload(payload)
    target = measurement_identity(changed, rig.cases, rig.ref.digest, "champion")
    assert target.to_payload()["target"]["settings_hash"] == "settings-after-the-change"
    admitted = import_sealed(
        rig.run_root,
        CAMPAIGN_RUN_ID,
        winner.measurement_id,
        target,
        expected_identity_digest=winner.identity.digest,
        primary=PRIMARY,
        condition_changed=["target"],
    )
    assert admitted.imported == 6
    assert admitted.fields == ("target",)
    assert {tuple(row["source"]["condition_fields"]) for row in admitted.rows} == {("target",)}


# -- provenance on imported rows (G2b1, family 12) ---------------------------

MAP = {"catalog": {"sha256": "a" * 64, "sources": ["skills/a/SKILL.md"]}}
ROW_KEYS = ("provenance", "provenance_overflow", "components_digest")


def born_with(admitted: Any, target: RunIdentityV1, *, run_id: str = "pred-2") -> SourceMeasurement:
    """A journal born with `admitted.rows` and nothing else: a transitive source."""

    records = [
        _envelope(
            1,
            "run_created",
            dict(
                identity=target.to_payload(),
                identity_digest=target.digest,
                producing_version="0.3.0a23",
            ),
        )
    ]
    for row in admitted.rows:
        records.append(_envelope(len(records) + 1, "sample_imported", dict(row)))
    return SourceMeasurement(
        run_id=run_id,
        measurement_id="m-r01-champion",
        identity_digest=target.digest,
        prefix_digest="prefix-digest",
        records=tuple(records),
    )


def test_a_marked_target_carries_the_source_rows_provenance():
    source = source_measurement(row_provenance=True, provenance=["a.md"], components=MAP)
    admitted = admit_rows(
        source, target_identity(row_provenance=True), primary="score", condition_changed=()
    )
    row = admitted.rows[0]
    assert row["provenance"] == ["a.md"] and row["components_digest"] == components_digest(MAP)
    assert admitted.pairs_digest == import_pairs_digest(list(admitted.rows))


def test_an_unmarked_target_carries_neither():
    source = source_measurement(row_provenance=True, provenance=["a.md"], components=MAP)
    unmarked = admit_rows(source, target_identity(), primary="score", condition_changed=())
    row = unmarked.rows[0]
    assert not set(ROW_KEYS) & row.keys()
    # The declaration of an unmarked import is what it was before family 12.
    plain = admit_rows(
        source_measurement(), target_identity(), primary="score", condition_changed=()
    )
    assert unmarked.rows[0]["source"].keys() == plain.rows[0]["source"].keys()
    assert {k: v for k, v in row.items() if k != "source"} == {
        k: v for k, v in plain.rows[0].items() if k != "source"
    }


def test_a_transitive_row_keeps_what_it_was_imported_with():
    # The journal grammar wants a hex `source.candidate_digest`.
    tree = "c" * 64
    marked = target_identity(row_provenance=True, candidate_digest=tree)
    source = source_measurement(
        row_provenance=True, provenance=["a.md"], components=MAP, candidate_digest=tree
    )
    first = admit_rows(source, marked, primary="score", condition_changed=())
    row = admit_rows(born_with(first, marked), marked, primary="score", condition_changed=()).rows[
        0
    ]
    assert row["provenance"] == first.rows[0]["provenance"] == ["a.md"]
    assert row["components_digest"] == components_digest(MAP)
    assert row["source"]["run_id"] == "pred-1"


def test_a_transitive_row_imported_without_provenance_stays_unknown():
    marked = target_identity(row_provenance=True)
    source = source_measurement(row_provenance=True, imported_from="grand-1", imported_fields=["x"])
    row = admit_rows(source, marked, primary="score", condition_changed=()).rows[0]
    assert not set(ROW_KEYS) & row.keys()


def test_an_unmarked_source_imports_unknown_provenance():
    row = admit_rows(
        source_measurement(),
        target_identity(row_provenance=True),
        primary="score",
        condition_changed=(),
    ).rows[0]
    assert not set(ROW_KEYS) & row.keys()


def test_an_overflowed_source_row_imports_as_unknown():
    source = source_measurement(row_provenance=True, provenance_overflow=True, components=MAP)
    row = admit_rows(
        source, target_identity(row_provenance=True), primary="score", condition_changed=()
    ).rows[0]
    assert "provenance" not in row and "provenance_overflow" not in row
    assert row["components_digest"] == components_digest(MAP)
