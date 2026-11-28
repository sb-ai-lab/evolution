"""Fixed evaluations record provenance through Core's measurement journal."""

from dataclasses import replace

import pytest
from evolution_lab._fixed_execution import build_fixed_identity
from evolution_lab.evaluation import evaluate_fixed
from evolution_lab.report import read_evaluation_report
from test_evaluation import bindings

from evolution.tree import RunIdentityV1, inspect_journal, measurement_provenance
from evolution.tree.execution.interaction import RolloutBudget, SemanticIdentity


@pytest.mark.parametrize("reports", [True, False])
async def test_fixed_evaluation_records_provenance(tmp_path, reports):
    kwargs = bindings(tmp_path, repeats=2)
    instance = kwargs["instance"]
    if reports:
        instance.components = {"skill": {"sha256": "a" * 64, "sources": ["instructions.txt"]}}
        original_send = instance.send

        async def send(*args):
            return replace(await original_send(*args), provenance={"used": ["skill"]})

        instance.send = send

    identity, _ = build_fixed_identity(
        candidate_digest=kwargs["candidate"].digest,
        target=SemanticIdentity(**kwargs["identity"].to_payload()["target"]),
        environment=kwargs["environment"],
        scorer=kwargs["scorer"],
        evaluation_set_id="synthetic",
        cases=kwargs["cases"],
        repeat_seeds=[1, 2],
        budget=RolloutBudget(max_turns=1, timeout_seconds=10),
        metric_plan={"primary": "quality"},
    )
    directory = kwargs["run_directory"]
    marked = await evaluate_fixed(**{**kwargs, "identity": identity})
    records = inspect_journal(directory).records
    assert records[0].payload["identity"]["row_provenance"] == 1
    events = [record.event for record in records]
    finished = [record.payload for record in records if record.event == "rollout_finished"]
    assert len(finished) == 2
    if reports:
        assert events.count("components_declared") == 1
        declaration = events.index("components_declared")
        assert 0 < declaration < events.index("rollout_started")
        digest = records[declaration].payload["components_digest"]
        assert all(
            row["provenance"] == ["skill"] and row["components_digest"] == digest
            for row in finished
        )
    else:
        assert "components_declared" not in events
        assert all(
            not {"provenance", "provenance_overflow", "components_digest"} & row.keys()
            for row in finished
        )
    assert measurement_provenance(directory).to_payload() == {
        "recorded": True,
        "reported": 2 if reports else 0,
        "overflow": 0,
        "not_reported": 0 if reports else 2,
        "components_declared": int(reports),
    }

    # The same measurement with a pre-marker identity still reads unchanged.
    old_directory = tmp_path / "unmarked"
    payload = identity.to_payload()
    del payload["row_provenance"]
    unmarked = await evaluate_fixed(
        **{
            **kwargs,
            "run_directory": old_directory,
            "identity": RunIdentityV1(payload),
        }
    )
    assert not measurement_provenance(old_directory).recorded
    # The physical journal references bind each run's own bytes.
    for result, root in ((marked, directory), (unmarked, old_directory)):
        snapshot = inspect_journal(root)
        core = result["core"]
        assert core.pop("prefix_digest") == snapshot.prefix_digest
        assert core.pop("valid_bytes") == snapshot.valid_bytes
        assert core.pop("last_sequence") == snapshot.sequence
        assert core.pop("run_identity_digest") == snapshot.records[0].payload["identity_digest"]
        [reference] = core.pop("references")
        assert reference["content_digest"] == snapshot.prefix_digest
    assert marked == unmarked
    assert (
        read_evaluation_report(directory).markdown()
        == read_evaluation_report(old_directory).markdown()
    )
