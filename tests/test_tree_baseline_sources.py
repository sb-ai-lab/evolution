"""Read-only authentication and compatibility of standalone baseline sources."""

from __future__ import annotations

import fcntl
import hashlib
import json

import pytest

from evolution.tree.baseline_import import BaselineSourceUnavailable
from evolution.tree.execution.journal import inspect_journal
from evolution.tree.execution.journal_identity import canonical_json
from tests.test_tree_baseline_import import source_measurement


def write_source(directory, *, kind="measurement", terminal=None, imported=False, marker=True):
    """Write genuine S1/owner envelopes without launching an agent or repairing a log."""
    source = source_measurement(
        imported_from="earlier" if imported else None, row_provenance=marker
    )
    rows = [record.to_payload() for record in source.records]
    identity = rows[0]["payload"]["identity"]
    if kind == "evaluation":
        identity["profile"] = "rollout_evaluation/1"
    else:
        identity["schedule"][0]["sample_id"] = "case-0-repeat-1"
        for row in rows[1:]:
            if "sample_id" in row["payload"]:
                row["payload"]["sample_id"] = "case-0-repeat-1"
    rows[0]["payload"]["identity_digest"] = hashlib.sha256(canonical_json(identity)).hexdigest()
    child = directory / "measurements" / "evaluation" if kind == "evaluation" else directory
    child.mkdir(parents=True)
    (child / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    if kind == "evaluation":
        configuration = {"profile": "evaluation/1", "split": "train"}

        def owner(event, payload):
            return {
                **rows[0],
                "run_id": "owner",
                "sequence": len(owner_rows) + 1,
                "event": event,
                "payload": payload,
            }

        owner_rows = []
        owner_rows.append(
            owner(
                "evaluation_created",
                {
                    "identity": configuration,
                    "identity_digest": hashlib.sha256(canonical_json(configuration)).hexdigest(),
                },
            )
        )
        owner_rows.append(owner("measurement_bound", rows[0]["payload"] | {}))
        del owner_rows[1]["payload"]["producing_version"]
        if terminal == "completed":
            owner_rows.append(
                owner(
                    "evaluation_completed",
                    {"measurement_prefix": inspect_journal(child).prefix_digest},
                )
            )
        elif terminal == "suspended":
            owner_rows.append(owner("evaluation_suspended", {"reason": "incomplete"}))
        (directory / "evaluation.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in owner_rows)
        )
    return directory


@pytest.mark.parametrize(
    "kind,terminal",
    [
        ("measurement", None),
        ("evaluation", None),
        ("evaluation", "completed"),
        ("evaluation", "suspended"),
    ],
)
def test_source_kinds_read(tmp_path, kind, terminal):
    from evolution.tree.baseline_sources import read_standalone_source

    directory = write_source(tmp_path / "source", kind=kind, terminal=terminal)
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    source = read_standalone_source(directory)
    assert source.kind == kind
    assert source.measurement.run_id == ("owner" if kind == "evaluation" else "measurement-run")
    assert source.measurement.measurement_id == (
        "evaluation" if kind == "evaluation" else "measurement-run"
    )
    assert source.journal_locator == (
        "measurements/evaluation/events.jsonl" if kind == "evaluation" else "events.jsonl"
    )
    assert source.split == ("train" if kind == "evaluation" else None)
    assert (source.configuration_digest is not None) == (kind == "evaluation")
    assert source.row_provenance is True
    assert {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(
    "fault",
    [
        "absent",
        "missing_child",
        "torn_owner",
        "torn_child",
        "owner_lease",
        "child_lease",
        "preflight",
        "campaign",
        "bare_evaluation",
        "imported",
        "binding_mismatch",
        "configuration_profile",
        "child_profile",
    ],
)
def test_source_unavailable_at_creation(tmp_path, fault):
    from evolution.tree.baseline_sources import read_standalone_source

    evaluation = fault in {
        "missing_child",
        "torn_owner",
        "owner_lease",
        "binding_mismatch",
        "configuration_profile",
        "child_profile",
    }
    directory = write_source(
        tmp_path / "source",
        kind="evaluation" if evaluation else "measurement",
        imported=fault == "imported",
    )
    child = directory / "measurements/evaluation" if evaluation else directory
    log = child / "events.jsonl"
    lock = None
    if fault == "absent":
        log.unlink()
    elif fault == "missing_child":
        log.unlink()
    elif fault in {"torn_owner", "torn_child"}:
        with (directory / "evaluation.jsonl" if fault == "torn_owner" else log).open(
            "ab"
        ) as stream:
            stream.write(b'{"schema_version":')
    elif fault in {"owner_lease", "child_lease"}:
        lock = (
            directory / "evaluation.lock" if fault == "owner_lease" else child / ".owner.lock"
        ).open("w")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        selected = (
            directory / "evaluation.jsonl"
            if fault in {"binding_mismatch", "configuration_profile"}
            else log
        )
        rows = [json.loads(line) for line in selected.read_text().splitlines()]
        created = rows[1 if fault == "binding_mismatch" else 0]["payload"]
        identity = created["identity"]
        if fault == "preflight":
            identity.update(profile="connection_preflight/1", case_subset=[0])
        elif fault == "campaign":
            identity["connection_binding"] = 1
        elif fault == "bare_evaluation":
            identity["profile"] = "rollout_evaluation/1"
        elif fault == "binding_mismatch":
            identity["candidate_digest"] = "other"
        elif fault == "configuration_profile":
            identity["profile"] = "other"
        elif fault == "child_profile":
            identity["profile"] = "rollout_measurement"
        created["identity_digest"] = hashlib.sha256(canonical_json(identity)).hexdigest()
        selected.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    try:
        with pytest.raises(BaselineSourceUnavailable):
            read_standalone_source(directory)
        assert {
            path: path.read_bytes() for path in directory.rglob("*") if path.is_file()
        } == before
    finally:
        if lock is not None:
            lock.close()


def comparison_source(tmp_path):
    from evolution.tree.baseline_sources import read_standalone_source

    return read_standalone_source(write_source(tmp_path / "source"))


def test_held_out_proof(tmp_path):
    from dataclasses import replace

    from evolution.tree.baseline_sources import held_out_finding

    source = comparison_source(tmp_path)
    train = {"mode": "explicit", "assignments": [["c", "train"], ["held", "test"]]}
    assert held_out_finding(replace(source, kind="evaluation", split="train"), train) is None
    assert held_out_finding(replace(source, kind="evaluation", split="test"), train) == {
        "split": "test",
        "cases": [],
    }
    assert held_out_finding(source, {"mode": "explicit", "assignments": [["c", "test"]]}) == {
        "split": None,
        "cases": ["c"],
    }
    assert (
        held_out_finding(source, {"mode": "implicit_train", "assignments": [["c", "train"]]})
        is None
    )


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("kind", "other", "identity.scorer.kind"),
        ("digest", "other", "identity.scorer.digest"),
        ("recovery", "replayable", "identity.scorer.recovery"),
        ("candidate_digest", "other", "identity.candidate_digest"),
        ("primary", "other", "identity.metric_plan_digest"),
    ],
)
def test_authorizable_field_classes(tmp_path, field, value, expected):
    from dataclasses import replace

    from evolution.tree.baseline_sources import authorizable_differences
    from evolution.tree.campaign_identity import metric_plan_digest
    from tests.test_tree_baseline_import import _envelope

    source = comparison_source(tmp_path)
    identity = dict(source.measurement.records[0].payload["identity"])
    identity["metric_plan_digest"] = metric_plan_digest("score")
    created = dict(source.measurement.records[0].payload, identity=identity)
    source = replace(
        source,
        measurement=replace(
            source.measurement,
            records=(
                _envelope(
                    1,
                    "run_created",
                    dict(
                        created,
                        identity_digest=hashlib.sha256(canonical_json(identity)).hexdigest(),
                    ),
                ),
            ),
        ),
    )
    kwargs = dict(
        candidate_digest=identity["candidate_digest"],
        scorer=dict(identity["scorer"]),
        primary="score",
    )
    assert authorizable_differences(source, **kwargs) == ()
    identity["metric_plan_digest"] = hashlib.sha256(
        canonical_json({"primary": "score"})
    ).hexdigest()
    source = replace(
        source,
        measurement=replace(
            source.measurement,
            records=(
                _envelope(
                    1,
                    "run_created",
                    dict(
                        created,
                        identity=identity,
                        identity_digest=hashlib.sha256(canonical_json(identity)).hexdigest(),
                    ),
                ),
            ),
        ),
    )
    assert authorizable_differences(source, **kwargs) == ()
    if field in ("kind", "digest", "recovery"):
        kwargs["scorer"][field] = value
    else:
        kwargs[field] = value
    differences = authorizable_differences(source, **kwargs)
    assert [difference.field for difference in differences] == [expected]


@pytest.mark.parametrize(
    "field,key,value,expected",
    [
        ("target", "runtime_ref_digest", "other", "identity.target.runtime_ref_digest"),
        ("environment", "digest", "other", "identity.environment.digest"),
        ("budget", "max_turns", 3, "identity.budget.max_turns"),
        (
            "materialization_strategy",
            None,
            "artifact_root_injection",
            "identity.materialization_strategy",
        ),
        ("max_concurrency", None, 2, None),
        ("max_scorer_concurrency", None, 2, None),
        ("sample_retries", None, 1, None),
    ],
)
def test_caveat_field_classes(tmp_path, field, key, value, expected):
    from evolution.tree.baseline_sources import caveat_fields
    from evolution.tree.execution.journal_identity import RunIdentityV1

    source = comparison_source(tmp_path)
    target = json.loads(json.dumps(source.measurement.records[0].payload["identity"]))
    if key is None:
        target[field] = value
    else:
        target[field][key] = value
    partition = {"mode": "implicit_train", "assignments": [["c", "train"]]}
    fields = caveat_fields(source, RunIdentityV1(target), partition)
    assert (expected in fields) if expected else fields == ()


def test_caveat_corpus_subset_and_schedule(tmp_path):
    from dataclasses import replace

    from evolution.tree.baseline_sources import caveat_fields
    from evolution.tree.execution.interaction import EvalCase
    from evolution.tree.execution.journal_identity import RunIdentityV1, corpus_digest
    from tests.test_tree_baseline_import import _envelope, target_identity

    source = comparison_source(tmp_path)
    cases = [EvalCase(name, name, "scenario", (), {}, {}, {}) for name in ("c", "new")]
    identity = json.loads(json.dumps(source.measurement.records[0].payload["identity"]))
    identity["corpus_digest"] = corpus_digest(cases[:1])
    source = replace(
        source,
        measurement=replace(
            source.measurement,
            records=(
                _envelope(
                    1,
                    "run_created",
                    dict(
                        identity=identity,
                        identity_digest=hashlib.sha256(canonical_json(identity)).hexdigest(),
                        producing_version="2.0.0b19",
                    ),
                ),
            ),
        ),
    )
    target = target_identity(cases=["c", "new"]).to_payload()
    target["corpus_digest"] = corpus_digest(cases)
    partition = {"mode": "implicit_train", "assignments": [["c", "train"], ["new", "train"]]}
    assert caveat_fields(source, RunIdentityV1(target), partition, corpus=cases) == (
        "identity.corpus_cases",
        "identity.schedule",
    )
    partition["assignments"] = [["c", "test"], ["new", "train"]]
    assert "identity.corpus_digest" in caveat_fields(
        source, RunIdentityV1(target), partition, corpus=cases[1:]
    )
