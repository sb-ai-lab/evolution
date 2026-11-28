"""Evaluation journals retain Target provenance and expose its coverage."""

import json

import pytest

from evolution.tree.affected_cases import baseline_provenance
from evolution.tree.evaluation import evaluate
from evolution.tree.execution.journal import inspect_journal
from evolution.tree.execution.journal_identity import IdentityError
from tests.campaign_fakes import FakeAdapter
from tests.test_tree_train_test import evaluation_manifest

pytestmark = pytest.mark.slow

COMPONENTS = {"skill": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}


def records_at(run):
    return inspect_journal(run / "measurements" / "evaluation").records


@pytest.mark.parametrize("overflow", [False, True])
async def test_evaluation_records_provenance(tmp_path, overflow):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    used = [f"{index:04d}-" + "x" * 251 for index in range(1024)] if overflow else ["skill"]
    result = await evaluate(
        path,
        run,
        adapter_factory=lambda *args: FakeAdapter(
            used=lambda case_id, turn: used, components=COMPONENTS
        ),
    )
    assert result["state"] == "completed"
    records = records_at(run)
    assert records[0].payload["identity"]["row_provenance"] == 1
    declarations = [row for row in records if row.event == "components_declared"]
    assert len(declarations) == 1
    finished = [row.payload for row in records if row.event == "rollout_finished"]
    assert len(finished) == 3
    for payload in finished:
        assert payload["components_digest"] == declarations[0].payload["components_digest"]
        if overflow:
            assert payload["provenance_overflow"] is True
            assert "provenance" not in payload
        else:
            assert payload["provenance"] == ["skill"]
            assert "provenance_overflow" not in payload
    assert [sample.used for sample in baseline_provenance(records)["test"]] == [
        None if overflow else ("skill",)
    ] * 3


async def test_evaluation_without_reports(tmp_path):
    run = tmp_path / "run"
    await evaluate(evaluation_manifest(tmp_path), run, adapter_factory=lambda *args: FakeAdapter())
    records = records_at(run)
    assert records[0].payload["identity"]["row_provenance"] == 1
    for row in records:
        if row.event == "rollout_finished":
            assert (
                not {"provenance", "provenance_overflow", "components_digest"} & row.payload.keys()
            )
    assert all(sample.used is None for sample in baseline_provenance(records)["test"])


@pytest.mark.parametrize("other_version", [False, True])
async def test_evaluation_marker_on_resume(tmp_path, other_version):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"

    def adapter(*args):
        return FakeAdapter(used=lambda case_id, turn: ["skill"], components=COMPONENTS)

    def stop_after_one():
        return (run / "measurements" / "evaluation" / "events.jsonl").exists() and any(
            row.event == "rollout_finished" for row in records_at(run)
        )

    first = await evaluate(path, run, cancelled=stop_after_one, adapter_factory=adapter)
    assert (first["state"], first["completed_samples"]) == ("suspended", 1)
    before = records_at(run)
    assert before[0].payload["identity"]["row_provenance"] == 1
    owner = [json.loads(line) for line in (run / "evaluation.jsonl").read_text().splitlines()]
    bound = next(row["payload"] for row in owner if row["event"] == "measurement_bound")
    assert bound["identity"] == before[0].payload["identity"]
    if other_version:
        log = run / "measurements" / "evaluation" / "events.jsonl"
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        rows[0]["payload"]["producing_version"] = "2.0.0b18"
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with pytest.raises(IdentityError):
            await evaluate(path, run, adapter_factory=adapter)
    else:
        second = await evaluate(path, run, adapter_factory=adapter)
        assert (second["state"], second["completed_samples"]) == ("completed", 3)
        after = records_at(run)
        assert [row.encode() for row in after[: len(before)]] == [row.encode() for row in before]
        assert all(
            row.payload["provenance"] == ["skill"]
            for row in after
            if row.event == "rollout_finished" and row.payload["outcome"] == "completed"
        )
        assert all(sample.used == ("skill",) for sample in baseline_provenance(after)["test"])
        assert sum(row.event == "components_declared" for row in after) == 1


EMPTY_COUNTS = {
    "reported": None,
    "overflow": None,
    "not_reported": None,
    "components_declared": None,
}
NOTE = (
    "evo tree evaluate: note: no sample carried provenance; a campaign with "
    "gate.provenance.kind reported treats every case as affected"
)


def scripted_reports(mode):
    reports = iter([["skill"], [f"{index:04d}-" + "x" * 251 for index in range(1024)], None])

    def used(case_id, turn):
        return next(reports) if mode == "mixed" else None

    return used


@pytest.mark.parametrize("mode", ["mixed", "none", "unmarked"])
def test_evaluation_inspection_provenance(tmp_path, monkeypatch, mode):
    from typer.testing import CliRunner

    from evolution.tree import evaluation
    from evolution.tree.cli import tree_app

    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    original = evaluation.evaluate
    if mode == "unmarked":
        constructor = evaluation.RunIdentityV1
        monkeypatch.setattr(
            evaluation,
            "RunIdentityV1",
            lambda data: constructor(
                {key: value for key, value in data.items() if key != "row_provenance"}
            ),
        )
    used = scripted_reports(mode)

    async def with_fake_adapter(*args, **kwargs):
        return await original(
            *args,
            **kwargs,
            adapter_factory=lambda *unused: FakeAdapter(
                used=used, components=COMPONENTS if mode == "mixed" else None
            ),
        )

    monkeypatch.setattr(evaluation, "evaluate", with_fake_adapter)
    invocation = CliRunner().invoke(
        tree_app, ["evaluate", "--manifest", str(path), "--run-dir", str(run)]
    )
    assert invocation.exit_code == 0, invocation.output
    report = json.loads(invocation.stdout)
    expected = {
        "mixed": {
            "recorded": True,
            "reported": 1,
            "overflow": 1,
            "not_reported": 1,
            "components_declared": 1,
        },
        "none": {
            "recorded": True,
            "reported": 0,
            "overflow": 0,
            "not_reported": 3,
            "components_declared": 0,
        },
        "unmarked": {"recorded": False, **EMPTY_COUNTS},
    }[mode]
    assert report["provenance"] == expected
    assert invocation.stderr.count(NOTE) == (1 if mode == "none" else 0)
    assert evaluation.inspect_evaluation(run) == report
    # Reopening a completed measurement also prints exactly one note and keeps its evidence.
    before = (run / "measurements" / "evaluation" / "events.jsonl").read_bytes()
    again = CliRunner().invoke(
        tree_app, ["evaluate", "--manifest", str(path), "--run-dir", str(run)]
    )
    assert again.exit_code == 0
    assert json.loads(again.stdout) == report
    assert again.stderr.count(NOTE) == (1 if mode == "none" else 0)
    assert (run / "measurements" / "evaluation" / "events.jsonl").read_bytes() == before


def test_measurement_provenance_reads_a_standalone_journal(tmp_path):
    from evolution.tree import measurement_provenance
    from evolution.tree.execution.journal import Journal
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from tests.tree.execution.test_journal_identity import cases, identity_payload

    data = identity_payload()
    data.update(row_provenance=1, sample_retries=1)
    data["schedule"] = [
        {"case_id": "c", "sample_id": f"s{n}", "repeat": n, "requested_seed": n}
        for n in range(1, 6)
    ]
    directory = tmp_path / "measurement"
    with Journal.create(directory, "run", RunIdentityV1(data), cases()) as journal:
        from types import SimpleNamespace

        from evolution.tree import declare_instance_components

        declare_instance_components(journal, SimpleNamespace(components=COMPONENTS))
        for index, extra in enumerate(
            [{"provenance": ["skill"]}, {"provenance": []}, {"provenance_overflow": True}, {}], 1
        ):
            common = {
                "case_id": "c",
                "sample_id": f"s{index}",
                "candidate_digest": data["candidate_digest"],
                "retry_ordinal": 0,
            }
            journal.append("rollout_started", common, operation_id=f"op-{index}")
            journal.append(
                "rollout_finished",
                {**common, "outcome": "cancelled" if index == 1 else "completed", **extra},
                operation_id=f"op-{index}",
            )
        journal.append(
            "rollout_started",
            {**common, "sample_id": "s1", "retry_ordinal": 1},
            operation_id="retry",
        )
        before = (directory / "events.jsonl").read_bytes()
        # Read while the writer owns its lease. No scoring records exist yet.
        assert measurement_provenance(directory).to_payload() == {
            "recorded": True,
            "reported": 1,
            "overflow": 1,
            "not_reported": 1,
            "components_declared": 1,
        }
        assert (directory / "events.jsonl").read_bytes() == before
    with (directory / "events.jsonl").open("ab") as stream:
        stream.write(b'{"torn":')
    assert measurement_provenance(directory).reported == 1
    assert (directory / "events.jsonl").read_bytes() == before + b'{"torn":'


@pytest.mark.parametrize("kind", ["missing", "empty", "invalid"])
def test_measurement_provenance_unavailable(tmp_path, kind):
    from evolution.tree import measurement_provenance

    directory = tmp_path / "measurement"
    if kind != "missing":
        directory.mkdir()
        (directory / "events.jsonl").write_bytes(b"" if kind == "empty" else b"{}\n")
    with pytest.raises(ValueError, match="^measurement journal unavailable$"):
        measurement_provenance(directory)


@pytest.mark.parametrize("bound", [False, True])
async def test_evaluation_provenance_before_measurement_birth(tmp_path, monkeypatch, bound):
    from evolution.tree import evaluation

    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"

    def interrupted(*args, **kwargs):
        raise RuntimeError("before measurement birth")

    if bound:
        monkeypatch.setattr(evaluation.Measurement, "create", interrupted)
    else:
        monkeypatch.setattr(evaluation, "_snapshot", interrupted)
    with pytest.raises(RuntimeError, match="before measurement birth"):
        await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert evaluation.inspect_evaluation(run)["provenance"] == {"recorded": bound, **EMPTY_COUNTS}


@pytest.mark.parametrize("born", [False, True])
@pytest.mark.parametrize("store_older", [False, True])
def test_legacy_evaluation_binding_refuses_before_version_check(
    tmp_path, monkeypatch, born, store_older
):
    import asyncio

    from typer.testing import CliRunner

    from evolution.tree import evaluation
    from evolution.tree.cli import tree_app

    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    child = run / "measurements" / "evaluation"
    constructor = evaluation.RunIdentityV1
    original = evaluation.evaluate

    def legacy_identity(data):
        return constructor({key: value for key, value in data.items() if key != "row_provenance"})

    def before_birth(*args):
        raise RuntimeError("bound before child birth")

    def stop_after_one():
        return (child / "events.jsonl").exists() and any(
            row.event == "rollout_finished" for row in records_at(run)
        )

    with monkeypatch.context() as legacy:
        legacy.setattr(evaluation, "RunIdentityV1", legacy_identity)
        if born:
            first = asyncio.run(
                original(
                    path,
                    run,
                    cancelled=stop_after_one,
                    adapter_factory=lambda *args: FakeAdapter(),
                )
            )
            assert (first["state"], first["completed_samples"]) == ("suspended", 1)
            records = [
                json.loads(line) for line in (child / "events.jsonl").read_text().splitlines()
            ]
            records[0]["payload"]["producing_version"] = "2.0.0b19"
            (child / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
        else:
            legacy.setattr(evaluation.Measurement, "create", before_birth)
            with pytest.raises(RuntimeError, match="bound before child birth"):
                asyncio.run(original(path, run, adapter_factory=lambda *args: FakeAdapter()))
            assert not (child / "events.jsonl").exists()
    if store_older:
        artifact_log = run / "artifacts" / "artifacts.jsonl"
        entries = [json.loads(line) for line in artifact_log.read_text().splitlines()]
        entries[0]["payload"]["producing_version"] = "2.0.0b18"
        artifact_log.write_text("".join(json.dumps(row) + "\n" for row in entries))
        if born:
            records[0]["payload"]["producing_version"] = "2.0.0b18"
            (child / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    owner = [json.loads(line) for line in (run / "evaluation.jsonl").read_text().splitlines()]
    bound = next(row["payload"]["identity"] for row in owner if row["event"] == "measurement_bound")
    assert "row_provenance" not in bound
    if born:
        assert records_at(run)[0].payload["identity"] == bound
    before = {file.relative_to(run): file.read_bytes() for file in run.rglob("*.jsonl")}

    async def with_fake_adapter(*args, **kwargs):
        return await original(*args, **kwargs, adapter_factory=lambda *unused: FakeAdapter())

    monkeypatch.setattr(evaluation, "evaluate", with_fake_adapter)
    invocation = CliRunner().invoke(
        tree_app, ["evaluate", "--manifest", str(path), "--run-dir", str(run)]
    )
    assert invocation.exit_code == (1 if store_older else 2), invocation.output
    assert invocation.stdout == ""
    if store_older:
        assert "evo tree evaluate: journal unavailable" in invocation.stderr
        assert "run_identity_changed" not in invocation.stderr
    else:
        assert "evo tree evaluate: refused: run_identity_changed" in invocation.stderr
    assert "invalid_identity" not in invocation.stderr
    assert "Traceback" not in invocation.output
    assert {file.relative_to(run): file.read_bytes() for file in run.rglob("*.jsonl")} == before
