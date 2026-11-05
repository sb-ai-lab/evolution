"""Public projections are deterministic, bounded views of accepted evidence."""

import hashlib
from dataclasses import replace

import pytest

from evolution.tree.execution.journal import Snapshot
from evolution.tree.execution.journal_projections import (
    Projection,
    project_events,
    project_result,
    project_status,
)
from evolution.tree.storage.envelope import SchemaError
from tests.tree.execution.test_journal_recovery import prefix


def snapshot(outcome=None, scoring=None, consistency="observed"):
    records = tuple(prefix(outcome, scoring))
    raw = b"".join(record.encode() for record in records)
    return Snapshot(
        records, consistency, hashlib.sha256(raw).hexdigest(), len(raw), True, "complete"
    )


@pytest.mark.parametrize(
    "outcome,scoring,want",
    [
        (None, None, None),
        ("completed", None, None),
        ("turn_budget", "started", None),
        ("completed", "finished", "completed"),
        ("turn_budget", "finished", "turn_budget"),
        ("completed", "failed", "scorer_failed"),
        ("completed", "interrupted", "interrupted"),
        *[
            (outcome, None, outcome)
            for outcome in (
                "time_budget",
                "target_failed",
                "cancelled",
                "interrupted",
                "environment_failed",
                "infrastructure_failed",
            )
        ],
    ],
)
def test_total_projection_dispositions(outcome, scoring, want):
    view = project_status(snapshot(outcome, scoring)).to_payload()
    sample = view["samples"][0]
    assert sample["execution_state"] == ("started" if outcome is None else "finished")
    assert (sample["evaluation"]["outcome"] if sample["evaluation"] else None) == want
    assert sample["scoring_state"] == (scoring or "not_started")
    assert sample["metrics"] == ({"accuracy": 1} if scoring == "finished" else None)
    assert Projection.from_payload(view).to_payload() == view


def test_repeat_reduction_and_consistency_are_explicit_without_caches(tmp_path):
    source = snapshot("completed", "finished")
    for reducer in (project_status, project_events, project_result):
        first = reducer(source).to_payload()
        (tmp_path / "result.json").write_text("garbage")
        (tmp_path / "result.json").unlink()
        assert reducer(source).to_payload() == first
        durable = reducer(replace(source, consistency="durable")).to_payload()
        assert durable | {"consistency": "observed"} == first


def test_references_bind_retained_prefix_not_canonical_reserialization():
    source = replace(snapshot("completed", "finished"), prefix_digest="b" * 64, valid_bytes=9000)
    result = project_result(source).to_payload()
    assert result["references"] == [
        dict(
            version=1,
            kind="journal_record",
            run_id=result["run_id"],
            locator="events.jsonl#bytes=0:9000",
            content_digest="b" * 64,
        )
    ]
    for field in ("campaign_outcome", "selection", "initial_champion", "final_champion"):
        assert result[field] is None
    assert result["claim_scope"] == "rollout_measurement"


def test_corrupt_suffix_is_lower_bound_and_never_authorizes_cleanup():
    source = snapshot()
    diagnostic = dict(
        scope="journal",
        code="journal_unavailable",
        offset=source.valid_bytes,
        category="corrupt_suffix",
        length=7,
        digest="b" * 64,
    )
    source = replace(
        source, complete=False, journal_integrity="corrupt_suffix", diagnostics=(diagnostic,)
    )
    result = project_status(source).to_payload()
    assert result["lower_bound"] is True
    assert result["recovery_actions"] == []
    assert result["samples"][0]["evaluation"] is None
    assert result["diagnostics"] == [diagnostic]


def test_unknown_creation_cannot_yield_authoritative_view():
    source = Snapshot((), "observed", "a" * 64, 0, False, "corrupt_suffix")
    with pytest.raises(SchemaError):
        project_status(source)


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p.update(schema_version=2),
        lambda p: p.update(kind="artifact"),
        lambda p: p.update(extra="SECRET"),
        lambda p: p["references"][0].update(kind="artifact"),
        lambda p: p["references"][0].update(locator="../private"),
        lambda p: p["samples"][0].update(details={"prompt": "SECRET"}),
    ],
)
def test_public_wire_rejects_extensions_and_sensitive_unknowns(edit):
    data = project_result(snapshot("completed", "finished")).to_payload()
    edit(data)
    with pytest.raises(SchemaError):
        Projection.from_payload(data)


def test_secret_diagnostics_and_nested_metric_names_rejected():
    source = snapshot("completed", "finished")
    with pytest.raises(SchemaError):
        project_events(source, secrets=("judge",))
    diagnostic = dict(
        scope="journal",
        code="journal_unavailable",
        offset=1,
        category="SECRET",
        length=7,
        digest="b" * 64,
    )
    with pytest.raises(SchemaError):
        project_status(replace(source, diagnostics=(diagnostic,)))
    result = project_result(source).to_payload()
    result["samples"][0]["metrics"] = {"SECRET": 1}
    with pytest.raises(SchemaError):
        Projection.from_payload(result, secrets=("SECRET",))


def test_wire_parser_rejects_duplicate_keys_and_constructor_aliasing():
    original = project_result(snapshot("completed", "finished"))
    assert Projection.from_bytes(original.encode()).to_payload() == original.to_payload()
    data = original.to_payload()
    copy = Projection(data)
    data["samples"][0]["metrics"]["accuracy"] = 99
    assert copy.to_payload()["samples"][0]["metrics"]["accuracy"] == 1
    encoded = original.encode().replace(
        b'"schema_version":1', b'"schema_version":1,"schema_version":1'
    )
    with pytest.raises(SchemaError):
        Projection.from_bytes(encoded)


@pytest.mark.parametrize(
    "outcome",
    [
        "completed",
        "turn_budget",
        "time_budget",
        "target_failed",
        "cancelled",
        "interrupted",
        "environment_failed",
        "infrastructure_failed",
    ],
)
@pytest.mark.parametrize("scoring", ["not_started", "started", "finished", "failed", "interrupted"])
def test_all_execution_scoring_combinations(outcome, scoring):
    from dataclasses import asdict

    from evolution.tree.execution.sample_outcomes import disposition_for

    source = project_status(snapshot("completed", "finished")).to_payload()
    sample = source["samples"][0]
    execution = disposition_for(outcome)
    sample["execution"] = asdict(execution)
    sample["scoring_state"] = scoring
    sample["metrics"] = {"accuracy": 1} if scoring == "finished" else None
    sample["critical_pass"] = None
    if execution.kind != "score":
        sample["evaluation"] = asdict(execution)
    elif scoring in ("not_started", "started"):
        sample["evaluation"] = None
    else:
        sample["evaluation"] = asdict(
            disposition_for(
                outcome
                if scoring == "finished"
                else "scorer_failed"
                if scoring == "failed"
                else "interrupted"
            )
        )
    if execution.kind != "score" and scoring != "not_started":
        with pytest.raises(SchemaError):
            Projection(source)
    else:
        Projection(source)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "kind",
        "run_id",
        "run_identity_digest",
        "candidate_digest",
        "last_sequence",
        "prefix_digest",
        "valid_bytes",
        "consistency",
        "complete",
        "journal_integrity",
        "diagnostics",
        "lower_bound",
        "references",
        "samples",
        "claim_scope",
    ],
)
@pytest.mark.parametrize("hostile", [[], {}, True, None])
def test_hostile_projection_types_are_safe(field, hostile):
    data = project_result(snapshot("completed", "finished")).to_payload()
    data[field] = hostile
    if field in ("complete", "lower_bound") and hostile is True:
        if field == "complete":
            return
    if field == "diagnostics" and hostile == []:
        return
    with pytest.raises(SchemaError):
        Projection(data)


def test_status_counts_tries_only_when_the_identity_retries():
    from tests.tree.execution.test_journal_recovery import prefix_retried

    records = tuple(prefix_retried(second_pending=False))
    raw = b"".join(record.encode() for record in records)
    source = Snapshot(
        records, "observed", hashlib.sha256(raw).hexdigest(), len(raw), True, "complete"
    )
    view = project_status(source).to_payload()
    (sample,) = view["samples"]
    assert sample["tries"] == 2 and sample["execution"]["outcome"] == "completed"
    assert Projection.from_payload(view).to_payload() == view
    assert (
        "tries" not in project_status(snapshot("completed", "finished")).to_payload()["samples"][0]
    )


def snapshot_with_one_import(retries=True):
    """A real measurement prefix whose one scheduled pair was carried in."""
    from tests.tree.execution.test_journal_schema import imported, ledger, retried_ledger

    state = retried_ledger() if retries else ledger()
    records = [state.created]
    record = imported(state, metrics={"score": 0.5}, critical_pass=True)
    state.accept(record)
    records.append(record)
    raw = b"".join(record.encode() for record in records)
    return Snapshot(
        tuple(records), "observed", hashlib.sha256(raw).hexdigest(), len(raw), True, "complete"
    )


def test_status_reports_an_imported_pair_as_imported_with_no_tries():
    view = project_status(snapshot_with_one_import()).to_payload()
    sample = next(s for s in view["samples"] if s["execution_state"] == "imported")
    assert sample["tries"] == 0 and sample["evaluation"]["kind"] == "score"
    assert sample["evaluation"]["outcome"] == "completed"
    assert (sample["scoring_state"], sample["metrics"], sample["critical_pass"]) == (
        "finished",
        {"score": 0.5},
        True,
    )
    assert Projection.from_payload(view).to_payload() == view
    plain = project_status(snapshot_with_one_import(retries=False)).to_payload()
    assert "tries" not in plain["samples"][0]
    assert plain["samples"][0]["execution_state"] == "imported"
    result = project_result(snapshot_with_one_import()).to_payload()
    assert Projection.from_payload(result).to_payload() == result


def test_an_imported_state_with_tries_is_refused():
    view = project_status(snapshot_with_one_import()).to_payload()
    view["samples"][0]["tries"] = 1
    with pytest.raises(SchemaError):
        Projection(view)


def test_stage_progress_never_counts_an_imported_pair_as_dispatched():
    """`sample_count` declares
    the rows a stage dispatches, so an imported pair is on neither side. A
    stage whose only row is carried declares 0 and is finished (it owes no
    try); a stage that declared a dispatched row is unfinished until that row
    starts, whatever carried rows the measurement holds."""

    from types import SimpleNamespace

    from evolution.tree.campaign_view import stage_progress

    status = project_status(snapshot_with_one_import()).to_payload()

    def view(sample_count: int) -> SimpleNamespace:
        return SimpleNamespace(
            snapshot=SimpleNamespace(
                measurements={"m": {"stages": ({"sample_count": sample_count},)}},
                # This stage has no outstanding repair.
                repair_owed=frozenset(),
                suspensions=(),
                family_version=14,
                retries_declined=frozenset(),
            ),
            measurements={"m": status},
            repairs={},
        )

    assert stage_progress(view(0), "m", retries=1) == (1, False)
    assert stage_progress(view(1), "m", retries=1) == (1, True)
