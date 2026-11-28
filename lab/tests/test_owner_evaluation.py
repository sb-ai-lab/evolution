"""S3 Task 3: installed fixed-evaluation worker and shared measurement path."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import threading
from threading import Thread
import time
import uuid

import pytest

from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
from ports import free_port

pytestmark = pytest.mark.slow


def _prepared(
    tmp_path: Path, monkeypatch, *, slow: bool = False,
    rollout_timeout_seconds: float | None = None,
    schema: str = "lab-evaluation-input/1", extra_defaults: dict | None = None,
    failing_score: bool = False,
    case_ids=("c-3", "c-1"), repeat_seeds=(17, 29), scorer_source=None, critical_cases=False,
):
    import evolution_lab.preparation.bundle as bundle_module
    from evolution_lab.preparation import EnvironmentInput, PreparationStore, describe_environment
    from test_preparation_evaluation import _evaluation_request

    capture_environment = bundle_module.capture_environment
    request = _evaluation_request(
        tmp_path, monkeypatch, schema=schema, extra_defaults=extra_defaults
    )
    monkeypatch.setattr(bundle_module, "capture_environment", capture_environment)
    root = request["source"].input_path.parent
    if critical_cases:
        corpus = root / 'evaluation.jsonl'
        rows = [json.loads(line) for line in corpus.read_text().splitlines()]
        corpus.write_text(''.join(json.dumps({**row, 'oracle': {'critical': True}}) + '\n' for row in rows))
    port = free_port()
    value = json.loads(request["source"].input_path.read_text())
    value["target"]["base_url"] = f"http://127.0.0.1:{port}"
    for name, digit in (
        ("contract_digest", "a"),
        ("settings_hash", "b"),
    ):
        value["target"][name] = digit * 64
    value["scorer"]["allowed_env"] += ["SCORER_LOG", "SLOW_SCORE"]
    value["defaults"]["repeat_seeds"] = list(repeat_seeds)
    if rollout_timeout_seconds is not None:
        value["defaults"]["timeout_seconds"] = rollout_timeout_seconds
    request["source"].input_path.write_text(json.dumps(value))
    launch = json.loads(request["source"].launch_path.read_text())
    launch["endpoint"]["base_url"] = value["target"]["base_url"]
    for name in ("contract_digest", "settings_hash"):
        launch["identity"][name] = value["target"][name]
    launch["identity"]["runtime_ref_digest"] = "c" * 64
    request["source"].launch_path.write_text(json.dumps(launch))
    shutil.copyfile(
        Path(__file__).parent / "owner_fixtures" / "launcher.py",
        request["source"].launch_path.parent / "launcher.py",
    )
    score_log = tmp_path / "scorer-calls.jsonl"
    (root / "scorer/score.py").write_text(
        scorer_source if scorer_source is not None else "import json, os, sys, time\n"
        "value=json.load(sys.stdin)\n"
        "with open(os.environ['SCORER_LOG'],'a') as stream:\n"
        " stream.write(json.dumps({'case_id':value['case_id']})+'\\n'); stream.flush()\n"
        f"if {failing_score!r}: raise RuntimeError('synthetic scorer failure')\n"
        "if os.environ.get('SLOW_SCORE') == 'yes': time.sleep(.5)\n"
        "json.dump({'metrics':{'score':1.0}},sys.stdout)\n"
    )
    request["env"].update(
        SCORER_LOG=str(score_log), SLOW_SCORE="yes" if slow else "no"
    )
    environment = EnvironmentInput(
        Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL), ()
    )
    descriptor = describe_environment(environment)
    launch = json.loads(request["source"].launch_path.read_text())
    launch["environment_ref"] = descriptor.digest
    request["source"].launch_path.write_text(json.dumps(launch))
    request["environments"] = (environment,)
    state = tmp_path / "state"
    store = PreparationStore(state)
    bundle = store.create_bundle(**request)
    prepared = store.prepare_evaluation(
        bundle.digest, selection={"case_ids": list(case_ids)}
    )
    return state, bundle, prepared, score_log


@pytest.mark.parametrize("limit, expected", [(None, 3), (1, 1), (2, 2), ("off", 4)])
def test_installed_worker_applies_saved_consecutive_unmeasured_limit(tmp_path, monkeypatch, limit, expected):
    from evolution_lab.owner import _evaluation_worker as worker

    # /1 omits the setting and must keep the historical stop after three.
    schema = "lab-evaluation-input/1" if limit is None else "lab-evaluation-input/3"
    defaults = {} if limit is None else {"max_consecutive_unmeasured": limit}
    state, bundle, prepared, score_log = _prepared(
        tmp_path, monkeypatch, schema=schema, extra_defaults=defaults, failing_score=True)
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    target = _Target(bound.bundle)
    try:
        assert worker.run_worker(config, _worker_environment(bound), lambda: False) == 0
    finally:
        target.close()
    outcome = json.loads(paths.outcome_path.read_text())
    assert prepared.evaluation.max_consecutive_unmeasured == (3 if limit is None else limit)
    assert outcome["coverage"] == {"expected": 4, "scored": 0}
    assert len(outcome["unmeasured"]) == expected
    assert outcome["stopped_reason"] == (None if limit == "off" else "consecutive_unmeasured")
    assert prepared.evaluation.sample_retries == 3
    assert outcome["evaluation_budget"] == {
        "max_failed_turns": 4, "call_retries": {"max_attempts": 3, "turn_max_attempts": 2}}
    assert len(score_log.read_text().splitlines()) == expected * 4


class _Target:
    def __init__(self, binding):
        from evolution.tree.target.reference_target import ReferenceBackend
        from evolution.tree.target.target_server import TargetServer, serve_target

        launch = binding.launch
        snapshots = binding.bundle_path.parent.parent / "target-snapshots"
        snapshots.mkdir(mode=0o700)
        self.server = TargetServer(
            ReferenceBackend(),
            allowed_artifact_root=binding.bundle_path.parent.parent / "artifacts",
            snapshot_root=snapshots,
            policy=binding.manifest.policy,
            **dict(launch["identity"]),
            **dict(launch["limits"]),
        )
        endpoint = binding.manifest.target.base_url.rsplit(":", 1)
        self.http = serve_target(self.server, "127.0.0.1", int(endpoint[1]))
        self.thread = Thread(
            target=self.http.serve_forever, kwargs={"poll_interval": .01}
        )
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)
        self.server.close()


def _operation(state: Path, bundle, prepared):
    from evolution_lab.owner.execution import (
        create_evaluation_paths,
        evaluation_worker_config,
    )
    from evolution_lab.owner.store import OwnerStateStore

    store = OwnerStateStore.open(state)
    generation = store.generation
    store.close()
    operation_id, scope_id = uuid.uuid4().hex, uuid.uuid4().hex
    paths = create_evaluation_paths(state, operation_id, scope_id)
    config = evaluation_worker_config(
        operation_id=operation_id,
        scope_id=scope_id,
        owner_generation=generation,
        bundle_digest=bundle.digest,
        prepared_digest=prepared.digest,
        state_directory=state,
        run_directory=paths.run_directory,
        outcome_path=paths.outcome_path,
        inputs_path=paths.inputs_path,
        settlement_path=paths.settlement_path,
        diagnostic_path=paths.diagnostic_path,
    )
    return paths, config


async def _helper_run(binding, paths, config, *, stop_after_score: Path | None = None):
    from evolution_lab.owner.execution import evaluation_worker_command
    from evolution_lab.owner.protocol import HelperHandle

    helper = HelperHandle.launch(
        sys.executable, paths.scope_directory, config["scope_id"],
        config["owner_generation"],
    )
    environment = dict(binding.base_environment)
    environment["SCORER_TOKEN"] = "private-neutral-scorer"
    await helper.configure(
        argv=evaluation_worker_command(sys.executable),
        env=environment,
        stop_grace_seconds=3,
        role="evaluation",
        child_config=config,
        cwd=str(paths.scope_directory),
    )
    await helper.wait_event("ready", timeout=3)
    await helper.go()
    await helper.wait_event("child_started", timeout=3)
    if stop_after_score is not None:
        async with asyncio.timeout(8):
            while not stop_after_score.exists():
                await asyncio.sleep(.01)
        await helper.stop()
    assert await helper.wait(timeout=20) == 0
    helper.close_commands()
    helper.close_target_ready()
    helper.close_events()
    return helper


def _opened(state: Path, prepared):
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.preparation.storage import PreparationStorage

    return open_prepared(PreparationStorage(state), prepared.digest)


async def _ready_target(owner, bundle):
    from evolution_lab.owner import TargetRef

    connection = await owner.bind(bundle.digest)
    task = await owner.start_target(
        connection, secrets={"TARGET_TOKEN": "private-neutral-target"}
    )
    status = await asyncio.wait_for(owner.wait(task), 8)
    assert status.state == "succeeded", status.to_payload()
    return TargetRef(status.ref.id, owner.owner_generation)


async def _finished(owner, ref, *, seconds: float = 20):
    async with asyncio.timeout(seconds):
        while True:
            value = await owner.status(ref)
            if value.control_state not in {"reserved", "running", "stopping"}:
                return value
            await asyncio.sleep(.02)


def test_installed_worker_measures_only_selected_cases_and_ordered_repeats(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution.tree import inspect_journal, project_result
    from evolution_lab.owner.settlement import read_evaluation_settlement
    from evolution_lab.owner.receipts import read_cleanup_receipt

    state, bundle, prepared, score_log = _prepared(tmp_path, monkeypatch)
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    target = _Target(bound.bundle)
    try:
        asyncio.run(_helper_run(bound.bundle, paths, config))
    finally:
        target.close()

    snapshot = inspect_journal(paths.measurement_directory)
    projection = project_result(snapshot).to_payload()
    inputs = json.loads(paths.inputs_path.read_text())
    assert [
        (row["case_id"], row["requested_seed"])
        for row in inputs["identity"]["schedule"]
    ] == [
        ("c-3", 17), ("c-3", 29), ("c-1", 17), ("c-1", 29),
    ]
    assert all(row["scoring_state"] == "finished" for row in projection["samples"])
    assert [json.loads(line)["case_id"] for line in score_log.read_text().splitlines()] == [
        "c-3", "c-3", "c-1", "c-1",
    ]
    outcome = json.loads(paths.outcome_path.read_text())
    assert outcome["schema"] == "lab-evaluation-outcome/3"
    assert outcome["min_coverage"] == 0.95 and outcome["unmeasured"] == []
    assert outcome["evaluation_budget"] == {
        "max_failed_turns": 4, "call_retries": {"max_attempts": 3, "turn_max_attempts": 2}}
    assert outcome["measurement_returned"] is True
    assert outcome["coverage"] == {"expected": 4, "scored": 4}
    assert outcome["stopped_reason"] is None
    settlement = read_evaluation_settlement(paths.settlement_path, **{
        name: config[name] for name in (
            "operation_id", "scope_id", "owner_generation", "run_id"
        )
    })
    assert settlement["normal_return"] is True
    assert settlement["materialize_started"] is True
    assert settlement["release_started"] is True
    assert settlement["schema"] == "lab-evaluation-settlement/2"
    assert settlement["ledger"]["namespace"] == config["run_id"]
    assert settlement["ledger"]["admission_closed"] is True
    assert settlement["ledger"]["quiescent"] is True
    assert settlement["ledger"]["uncertain"] == 0
    assert settlement["ledger"]["bound_transports"] >= 1
    assert settlement["ledger"]["admitted"] >= 2
    receipt = read_cleanup_receipt(
        paths.scope_directory, config["scope_id"], config["owner_generation"]
    )
    assert receipt["child_exit_code"] == 0 and receipt["forced"] is False
    assert set(path.name for path in paths.run_directory.iterdir()) == {
        "artifacts", "measurement",
    }
    assert paths.inputs_path.parent == paths.scope_directory
    assert inputs["schema"] == "lab-evaluation-inputs/1"
    assert inputs["identity"] == snapshot.records[0].payload["identity"]
    assert inputs["identity"]["row_provenance"] == 1
    assert inputs["identity"]["candidate_digest"] == projection["candidate_digest"]
    assert {item["name"] for item in inputs["distributions"]} == {
        "evo-skills", "evolution-lab",
    }


def test_installed_worker_records_the_declared_evaluation_budget(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner.execution import read_evaluation_outcome

    state, bundle, prepared, _ = _prepared(
        tmp_path, monkeypatch, schema="lab-evaluation-input/2",
        extra_defaults={
            "max_failed_turns": 2,
            "call_retries": {"max_attempts": 2, "turn_max_attempts": 1},
        },
    )
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    target = _Target(bound.bundle)
    try:
        asyncio.run(_helper_run(bound.bundle, paths, config))
    finally:
        target.close()

    outcome = read_evaluation_outcome(paths.outcome_path, **{
        name: config[name] for name in ("operation_id", "scope_id", "run_id")
    })
    assert outcome["schema"] == "lab-evaluation-outcome/3"
    assert outcome["min_coverage"] == 0.95 and outcome["unmeasured"] == []
    assert outcome["evaluation_budget"] == {
        "max_failed_turns": 2,
        "call_retries": {"max_attempts": 2, "turn_max_attempts": 1},
    }
    assert outcome["coverage"] == {"expected": 4, "scored": 4}
    assert outcome["stopped_reason"] is None


_OUTCOME_IDS = {"operation_id": "a" * 32, "scope_id": "b" * 32, "run_id": "run"}


def _outcome_document(**changes):
    value = {
        "schema": "lab-evaluation-outcome/3", "min_coverage": 0.95, "unmeasured": [], **_OUTCOME_IDS, "measurement_returned": True,
        "coverage": {"expected": 1, "scored": 1}, "stopped_reason": None,
        "evaluation_budget": {"max_failed_turns": 1, "call_retries": None},
    }
    value.update(changes)
    return {name: item for name, item in value.items() if item is not ...}


@pytest.mark.parametrize("changes", [
    {"schema": "lab-evaluation-outcome/1"},
    {"schema": "lab-evaluation-outcome/1", "evaluation_budget": ...},
    {"min_coverage": ...},
    {"min_coverage": None},
    {"min_coverage": True},
    {"min_coverage": 10**400},
    {"min_coverage": 0},
    {"min_coverage": float("nan")},
    {"unmeasured": ...},
    {"unmeasured": [{"case_id": "c", "sample_id": "s", "outcome": "time_budget", "tries": 0}]},
    {"evaluation_budget": ...},
    {"evaluation_budget": None},
    {"evaluation_budget": {"max_failed_turns": 0, "call_retries": None}},
    {"evaluation_budget": {"max_failed_turns": True, "call_retries": None}},
    {"evaluation_budget": {"max_failed_turns": 1}},
    {"evaluation_budget": {"max_failed_turns": 1, "call_retries": {"max_attempts": 11, "turn_max_attempts": 1}}},
    {"evaluation_budget": {"max_failed_turns": 1, "call_retries": {"max_attempts": 2}}},
])
def test_evaluation_outcome_reader_refuses_old_or_budgetless_documents(tmp_path: Path, changes) -> None:
    from evolution_lab.owner.execution import read_evaluation_outcome
    from evolution_lab.owner.receipts import ReceiptError

    path = tmp_path / "outcome.json"
    path.write_text(json.dumps(_outcome_document(**changes)))
    with pytest.raises(ReceiptError):
        read_evaluation_outcome(path, **_OUTCOME_IDS)


@pytest.mark.parametrize("unmeasured_count", [0, 400, 1200])
@pytest.mark.parametrize("publication", ["direct", "worker"])
def test_evaluation_outcome_reader_accepts_a_declared_budget(tmp_path: Path, unmeasured_count, publication) -> None:
    from evolution_lab.owner.execution import read_evaluation_outcome

    budget = {"max_failed_turns": 3, "call_retries": {"max_attempts": 2, "turn_max_attempts": 1}}
    path = tmp_path / "outcome.json"
    unmeasured = [{"case_id": "neutral-case", "sample_id": f"sample-{index}",
                   "outcome": "scorer_failed", "tries": 1} for index in range(unmeasured_count)]
    expected = 20 * unmeasured_count or 1
    document = _outcome_document(evaluation_budget=budget, unmeasured=unmeasured,
                                coverage={"expected": expected, "scored": expected - unmeasured_count})
    if publication == "worker":
        from evolution_lab.owner._evaluation_worker import _publish_outcome
        _publish_outcome({**_OUTCOME_IDS, "outcome_path": path}, document, budget=budget)
        assert path.exists(), "complete evaluation outcome must be published"
    else:
        path.write_text(json.dumps(document))
    value = read_evaluation_outcome(path, **_OUTCOME_IDS)
    assert value["evaluation_budget"] == budget
    assert value["min_coverage"] == 0.95 and value["unmeasured"] == unmeasured
    from evolution_lab.projects.facts import baseline_failure
    from types import SimpleNamespace
    assert baseline_failure(SimpleNamespace(kind="evaluation", control_state="finished",
        result={**value, "persistence": {"state": "saved"}})) is None
    legacy = _outcome_document(schema="lab-evaluation-outcome/2", min_coverage=..., unmeasured=...,
                               stopped_reason="cancelled")
    path.write_text(json.dumps(legacy))
    value = read_evaluation_outcome(path, **_OUTCOME_IDS)
    assert value["min_coverage"] == 1.0 and value["unmeasured"] is None
    assert value["stopped_reason"] == "cancelled"


def test_cooperative_stop_during_score_prevents_the_next_sample(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution.tree import inspect_journal, project_result

    state, bundle, prepared, score_log = _prepared(tmp_path, monkeypatch, slow=True)
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    target = _Target(bound.bundle)
    try:
        asyncio.run(_helper_run(bound.bundle, paths, config, stop_after_score=score_log))
    finally:
        target.close()
    projection = project_result(inspect_journal(paths.measurement_directory)).to_payload()
    assert sum(row["scoring_state"] == "finished" for row in projection["samples"]) == 1
    assert len(score_log.read_text().splitlines()) == 1
    outcome = json.loads(paths.outcome_path.read_text())
    assert outcome["coverage"] == {"expected": 4, "scored": 1}
    assert outcome["stopped_reason"] == "cancelled"


def test_stop_completed_during_artifact_setup_prevents_materialize(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution.tree.artifacts.artifacts import ArtifactStore
    from evolution.tree.target.http_adapter import HttpSessionAdapter
    from evolution_lab.owner._evaluation_worker import run_worker
    from evolution_lab.owner.settlement import read_evaluation_settlement

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    real_champion = ArtifactStore.champion
    real_materialize = HttpSessionAdapter.materialize
    stopping = {"value": False}
    materialize_calls = []

    def stop_after_artifact_setup(self):
        result = real_champion(self)
        stopping["value"] = True
        return result

    async def record_materialize(self, candidate, strategy, instance_id):
        materialize_calls.append(instance_id)
        return await real_materialize(self, candidate, strategy, instance_id)

    monkeypatch.setattr(ArtifactStore, "champion", stop_after_artifact_setup)
    monkeypatch.setattr(HttpSessionAdapter, "materialize", record_materialize)
    target = _Target(bound.bundle)
    try:
        environment = dict(bound.bundle.base_environment)
        environment["SCORER_TOKEN"] = "private-neutral-scorer"
        assert run_worker(config, environment, lambda: stopping["value"]) == 0
    finally:
        target.close()

    assert materialize_calls == []
    assert not paths.measurement_directory.exists()
    outcome = json.loads(paths.outcome_path.read_text())
    assert outcome["measurement_returned"] is False
    assert outcome["coverage"] is None
    assert outcome["stopped_reason"] == "cancelled"
    settlement = read_evaluation_settlement(paths.settlement_path, **{
        name: config[name] for name in (
            "operation_id", "scope_id", "owner_generation", "run_id"
        )
    })
    assert settlement["normal_return"] is True
    assert settlement["materialize_started"] is False
    assert settlement["release_started"] is False
    assert settlement["ledger"]["admitted"] == 0
    assert settlement["ledger"]["quiescent"] is True
    assert settlement["ledger"]["bound_transports"] == 1


@pytest.mark.parametrize("secret", ["private-neutral-scorer", "s3cr"])
def test_lost_release_response_preserves_measurement_and_forbids_reuse(
    tmp_path: Path, monkeypatch, capsys, secret
) -> None:
    from evolution.tree import inspect_journal, project_result
    from evolution.tree.target.http_adapter import HttpSessionAdapter
    from evolution_lab.owner._evaluation_worker import run_worker
    from evolution_lab.owner.settlement import (
        positive_settlement,
        read_evaluation_settlement,
    )

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    real_release = HttpSessionAdapter.release

    async def lost_release(self, instance_id):
        await real_release(self, instance_id)
        raise TimeoutError(secret)

    monkeypatch.setattr(HttpSessionAdapter, "release", lost_release)
    target = _Target(bound.bundle)
    try:
        environment = dict(bound.bundle.base_environment)
        environment["SCORER_TOKEN"] = secret
        assert run_worker(config, environment, lambda: False) == 1
    finally:
        target.close()
    captured = capsys.readouterr()
    assert "Traceback (most recent call last):" in captured.err
    assert "TimeoutError" in captured.err
    assert secret not in captured.err + captured.out + paths.diagnostic_path.read_text()
    projection = project_result(inspect_journal(paths.measurement_directory)).to_payload()
    assert sum(row["scoring_state"] == "finished" for row in projection["samples"]) == 4
    assert json.loads(paths.outcome_path.read_text())["measurement_returned"] is True
    settlement = read_evaluation_settlement(paths.settlement_path, **{
        name: config[name] for name in (
            "operation_id", "scope_id", "owner_generation", "run_id"
        )
    })
    assert settlement["normal_return"] is False
    assert settlement["release_started"] is True
    # The response was received by core, so the ledger classes the call completed;
    # the lost response is Lab-visible only as an abnormal return, which alone
    # forbids reuse.
    assert settlement["ledger"]["quiescent"] is True
    assert positive_settlement(
        settlement, run_id=config["run_id"], kind="evaluation"
    ) is False


def test_evaluation_failure_before_binding_still_publishes_settlement(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from evolution_lab.owner import _evaluation_worker as worker
    from evolution_lab.owner.settlement import (
        positive_settlement,
        read_evaluation_settlement,
    )

    from types import SimpleNamespace
    paths = SimpleNamespace(diagnostic_path=tmp_path / "diagnostic.json",
                            settlement_path=tmp_path / "settlement.json")
    config = {"operation_id": "a" * 32, "scope_id": "b" * 32,
              "owner_generation": "c" * 32, "run_id": "neutral-run",
              "diagnostic_path": str(paths.diagnostic_path), "settlement_path": str(paths.settlement_path)}

    def broken(*args, **kwargs):
        raise RuntimeError("private binding failure")

    monkeypatch.setattr(worker, "_bindings", broken)
    assert worker.run_worker(config, {}, lambda: False) == 1
    assert capsys.readouterr().err == "RuntimeError\n"
    assert paths.diagnostic_path.exists()
    settlement = read_evaluation_settlement(paths.settlement_path, **{
        name: config[name] for name in (
            "operation_id", "scope_id", "owner_generation", "run_id"
        )
    })
    assert settlement["normal_return"] is False
    assert settlement["materialize_started"] is False
    assert settlement["ledger"]["namespace"] is None
    assert settlement["ledger"]["admission_closed"] is True
    assert settlement["ledger"]["bound_transports"] == 0
    assert positive_settlement(
        settlement, run_id=config["run_id"], kind="evaluation"
    ) is False


@pytest.mark.parametrize("phase", ["redactions", "evaluation", "short_secret", "empty_redactions", "overlap"])
def test_evaluation_exception_log_redacts_supplied_environment(tmp_path, monkeypatch, capsys, phase):
    """Run real evaluation/settlement coroutines with controlled admitted bindings."""
    from types import SimpleNamespace
    from evolution_lab.owner import _evaluation_worker as worker
    secret = "s3cr" if phase == "short_secret" else "neutral-evaluation-credential"
    source = tmp_path / "source"
    source.mkdir()
    config = {"operation_id": "a" * 32, "scope_id": "b" * 32,
              "owner_generation": "c" * 32, "run_id": "neutral-run",
              "run_directory": str(tmp_path / "run"),
              "diagnostic_path": str(tmp_path / "diagnostic.json"),
              "settlement_path": str(tmp_path / "settlement.json")}
    manifest = SimpleNamespace(seed_root=source, policy=None, target=SimpleNamespace(timeouts=SimpleNamespace(
        connect_seconds=.01, read_seconds=.01)))
    prepared = SimpleNamespace(evaluation=SimpleNamespace(max_failed_turns=1, call_retries=None))
    monkeypatch.setattr(worker, "_bindings", lambda *args: (prepared, manifest, (), None, None))
    def broken(*args, **kwargs):
        message = "left|abcd|right" if phase == "overlap" else secret
        raise RuntimeError("public evaluation failure" if phase == "empty_redactions" else message)
    if phase == "redactions":
        monkeypatch.setattr(worker, "exception_redactions", broken)
    monkeypatch.setattr(worker, "scan_tree", broken)
    environ = {} if phase == "empty_redactions" else {"TARGET_TOKEN": secret}
    if phase == "overlap":
        environ = {"TARGET_TOKEN": "abc", "OTHER_TOKEN": "bcd"}
    assert worker.run_worker(config, environ, lambda: False) == 1
    captured = capsys.readouterr()
    if phase == "overlap":
        payload = captured.err.split("left|", 1)[1].split("|right", 1)[0]
        assert not set("abcd") & set(payload)
        assert all(value not in captured.err + Path(config["diagnostic_path"]).read_text()
                   for value in environ.values())
    assert secret not in captured.err + captured.out + Path(config["diagnostic_path"]).read_text()
    if phase == "redactions":
        assert captured.err == "RuntimeError\n"
    else:
        assert "Traceback (most recent call last):" in captured.err
        assert "RuntimeError" in captured.err
    settlement = json.loads(Path(config["settlement_path"]).read_text())
    assert settlement["normal_return"] is False
    assert settlement["ledger"]["admission_closed"] is True


def test_settlement_is_published_before_loop_teardown(
    tmp_path: Path, monkeypatch
) -> None:
    """A mutating call abandoned while its executor thread is still in socket I/O
    keeps `asyncio.run` teardown waiting; the settlement must already be on disk."""
    from evolution_lab.owner import _evaluation_worker as worker
    from evolution_lab.owner.settlement import (
        positive_settlement,
        read_evaluation_settlement,
    )

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    entered = threading.Event()
    unblock = threading.Event()
    stopping = {"value": False}
    real_adapter = worker.HttpSessionAdapter

    class AbandoningAdapter(real_adapter):
        async def materialize(self, *args):
            call = asyncio.ensure_future(super().materialize(*args))
            assert await asyncio.to_thread(entered.wait, 5)
            stopping["value"] = True
            call.cancel()
            try:
                await call
            except asyncio.CancelledError:
                pass
            raise TimeoutError("materialize abandoned")

    monkeypatch.setattr(worker, "HttpSessionAdapter", AbandoningAdapter)
    monkeypatch.setattr(worker, "settle_timeout", lambda binding: .3)
    target = _Target(bound.bundle)
    real_dispatch = target.server.dispatch

    def blocking_dispatch(method, path, raw_body=b""):
        if method == "POST" and path == "/evolution/v2/instances":
            entered.set()
            unblock.wait(10)
        return real_dispatch(method, path, raw_body)

    target.server.dispatch = blocking_dispatch
    outcome = {}
    environment = dict(bound.bundle.base_environment)
    environment["SCORER_TOKEN"] = "private-neutral-scorer"
    runner = Thread(target=lambda: outcome.setdefault(
        "code", worker.run_worker(config, environment, lambda: stopping["value"])
    ))
    try:
        runner.start()
        deadline = time.monotonic() + 8
        while not paths.settlement_path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert paths.settlement_path.exists()
        assert runner.is_alive(), "settlement must not wait for loop teardown"
        settlement = read_evaluation_settlement(paths.settlement_path, **{
            name: config[name] for name in (
                "operation_id", "scope_id", "owner_generation", "run_id"
            )
        })
        ledger = settlement["ledger"]
        assert settlement["normal_return"] is False
        assert settlement["materialize_started"] is True
        # Settle timed out with the abandoned call's socket I/O still open.
        assert ledger["admission_closed"] is True
        assert ledger["quiescent"] is False
        assert ledger["in_flight"] >= 1 and ledger["pending"] >= 1
        assert positive_settlement(
            settlement, run_id=config["run_id"], kind="evaluation"
        ) is False
    finally:
        unblock.set()
        runner.join(10)
        target.close()
    assert not runner.is_alive()
    assert outcome["code"] == 1


def _published_settlement(paths, config):
    from evolution_lab.owner.settlement import read_evaluation_settlement

    return read_evaluation_settlement(paths.settlement_path, **{
        name: config[name] for name in (
            "operation_id", "scope_id", "owner_generation", "run_id"
        )
    })


def _worker_environment(bound):
    environment = dict(bound.bundle.base_environment)
    environment["SCORER_TOKEN"] = "private-neutral-scorer"
    return environment


def test_worker_turn_cancelled_after_its_send_started_publishes_uncertain(
    tmp_path: Path, monkeypatch
) -> None:
    """The core rollout deadline cancels a `send` whose worker thread is already
    inside the target call; the reply arrives later, so the call is uncertain."""
    from evolution_lab.owner import _evaluation_worker as worker
    from evolution_lab.owner.settlement import positive_settlement
    from ledger_faults import HeldCall, LedgerProbe, abandoned_pending_turn, is_turn

    state, bundle, prepared, _ = _prepared(
        tmp_path, monkeypatch, rollout_timeout_seconds=1.5,
        schema="lab-evaluation-input/2", extra_defaults={"sample_retries": 0, "call_retries": "off"},
    )
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    probe = LedgerProbe(monkeypatch, worker)
    target = _Target(bound.bundle)
    held = HeldCall(target.server, is_turn)
    outcome = {}
    runner = Thread(target=lambda: outcome.setdefault(
        "code", worker.run_worker(config, _worker_environment(bound), lambda: False)
    ))
    try:
        runner.start()
        assert held.entered.wait(10)
        assert probe.wait_for(abandoned_pending_turn, seconds=10)
        held.release.set()
        runner.join(30)
        assert not runner.is_alive()
    finally:
        held.release.set()
        runner.join(30)
        target.close()
    assert outcome["code"] == 0
    result = json.loads(paths.outcome_path.read_text())
    # A single timed-out sample is unmeasured; later samples still run.
    assert result["stopped_reason"] is None
    assert result["coverage"] == {"expected": 4, "scored": 3}
    assert result["min_coverage"] == 0.95
    assert len(result["unmeasured"]) == 1 and result["unmeasured"][0]["outcome"] == "time_budget"
    settlement = _published_settlement(paths, config)
    # Everything but the ledger is positive: its uncertainty alone forbids reuse.
    assert settlement["normal_return"] is True
    assert settlement["materialize_started"] is settlement["release_started"] is True
    ledger = settlement["ledger"]
    assert ledger["namespace"] == config["run_id"]
    assert ledger["uncertain_any"] is True
    assert ledger["uncertain"] >= 1
    assert ledger["quiescent"] is True
    assert ledger["bound_transports"] >= 1
    [snapshot] = probe.settled
    assert [
        (record.operation, record.state, record.abandoned, record.reason)
        for record in snapshot.records
    ] == [("turn", "uncertain", True, "abandoned")]
    assert positive_settlement(
        settlement, run_id=config["run_id"], kind="evaluation"
    ) is False


def test_worker_transport_failure_on_turn_publishes_uncertain(
    tmp_path: Path, monkeypatch
) -> None:
    """The target reads a turn request and closes the connection without a reply."""
    from evolution_lab.owner import _evaluation_worker as worker
    from evolution_lab.owner.settlement import positive_settlement
    from ledger_faults import LedgerProbe, drop_first_reply, is_turn

    # Isolate one uncertain send, without a recovery call or sample attempt.
    state, bundle, prepared, _ = _prepared(
        tmp_path, monkeypatch, schema="lab-evaluation-input/2",
        extra_defaults={"sample_retries": 0, "call_retries": "off"})
    bound = _opened(state, prepared)
    paths, config = _operation(state, bundle, prepared)
    probe = LedgerProbe(monkeypatch, worker)
    dropped = drop_first_reply(monkeypatch, is_turn)
    target = _Target(bound.bundle)
    try:
        assert worker.run_worker(config, _worker_environment(bound), lambda: False) == 0
    finally:
        target.close()
    assert len(dropped) == 1
    settlement = _published_settlement(paths, config)
    assert settlement["normal_return"] is True
    assert settlement["materialize_started"] is settlement["release_started"] is True
    ledger = settlement["ledger"]
    assert ledger["uncertain"] >= 1
    assert ledger["uncertain_any"] is True
    assert ledger["quiescent"] is True
    [snapshot] = probe.settled
    assert [
        (record.operation, record.state, record.abandoned, record.reason)
        for record in snapshot.records
    ] == [("turn", "uncertain", False, "transport_error")]
    assert positive_settlement(
        settlement, run_id=config["run_id"], kind="evaluation"
    ) is False


async def test_legacy_evaluate_fixed_keeps_its_diagnostic_wrapper(tmp_path: Path) -> None:
    from evolution_lab.evaluation import evaluate_fixed
    from test_evaluation import bindings

    values = bindings(tmp_path, repeats=2)
    result = await evaluate_fixed(**values)
    assert result["coverage"] == {"expected": 2, "scored": 2}
    assert json.loads((values["run_directory"] / "evaluation-result.json").read_text()) == {
        "version": "lab-evaluation-result/2",
        "run_id": "evaluation",
        "coverage": {"expected": 2, "scored": 2},
        "unmeasured": [], "min_coverage": 1.0,
        "stopped_reason": None,
    }


def test_owner_runs_evaluation_and_projects_public_measurement(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, ResourceRef

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            boot_id = (await owner.target(target)).boot_id
            accepted = await owner.start(
                prepared.digest,
                target_ref=target,
                secrets={"SCORER_TOKEN": "private-neutral-scorer"},
            )
            ref = ResourceRef("operation", accepted.id)
            value = await _finished(owner, ref)
            assert value.kind == "evaluation"
            assert value.control_state == "finished", value.to_payload()
            assert value.process.exit_code == 0
            assert value.process.cleanup_complete is True
            assert value.target_cleanup.complete is True
            assert value.results_state == "present"
            assert value.result.kind == "evaluation"
            assert value.result.coverage.to_payload() == {"expected": 4, "scored": 4}
            assert value.result.stopped_reason is None
            assert value.result.min_coverage == 0.95 and value.result.to_payload()["unmeasured"] == []
            assert value.result.campaign_execution is None
            assert value.result.core is not None
            report = await owner.report(ref)
            assert report.state == "available"
            report_payload = report.to_payload()
            assert report_payload["schema"] == "lab-owner-report/15"
            assert report_payload["report"]["operation"] == "evaluation"
            assert report_payload["report"]["core"] == value.result.to_payload()["core"]
            assert report_payload["report"]["lab_result"] == {
                "coverage": {"expected": 4, "scored": 4},
                "stopped_reason": None, "unmeasured": [], "min_coverage": 0.95,
                "persistence": {"state": "saved", "failure": None},
            }
            assert report_payload["report"]["measurements"][0]["coverage"] == {
                "expected": 4, "scored": 4, "state": "complete",
            }
            assert report.starts == value.starts
            assert report_payload["usage"][0]["usage_state"] == "present"
            markdown = await owner.report_markdown(ref)
            assert markdown.state == "available"
            from evolution_lab.report import read_evaluation_report
            assert markdown.markdown == read_evaluation_report(
                state / "artifacts" / ref.id / ref.id / "measurement",
                lab_result={key: value.result.to_payload()[key] for key in
                            ("coverage", "stopped_reason", "unmeasured", "min_coverage", "persistence")}).markdown()
            (measured,) = (await owner.measurements(ref))["measurements"]
            assert measured["usage_state"] == "present"  # facts re-keyed to core's run id
            assert (await owner.target(target)).boot_id == boot_id
            assert (await owner.target(target)).state == "ready"

    asyncio.run(scenario())


def test_owner_stop_before_go_keeps_target_and_reports_unknown_optional_facts(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.protocol import HelperHandle

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    reached_ready = asyncio.Event()
    release_ready = asyncio.Event()
    real_wait_event = HelperHandle.wait_event

    async def held_ready(self, kind, *args, **kwargs):
        value = await real_wait_event(self, kind, *args, **kwargs)
        if kind == "ready":
            reached_ready.set()
            await release_ready.wait()
        return value

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            monkeypatch.setattr(HelperHandle, "wait_event", held_ready)
            accepted = await owner.start(
                prepared.digest,
                target_ref=target,
                secrets={"SCORER_TOKEN": "private-neutral-scorer"},
            )
            ref = ResourceRef("operation", accepted.id)
            await asyncio.wait_for(reached_ready.wait(), 8)
            assert (await owner.stop(ref)).effect == "requested"
            release_ready.set()
            value = await _finished(owner, ref)
            assert value.control_state == "finished", value.to_payload()
            assert value.process.launched is False
            assert value.result.core is None
            assert value.result.coverage is None
            assert value.result.stopped_reason == "unknown"
            assert value.results_state == "not_created"
            assert value.target_cleanup.complete is True
            assert (await owner.target(target)).state == "ready"

    asyncio.run(scenario())


def test_owner_partial_stop_holds_shared_exclusion_and_reuses_target(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, OwnerError, ResourceRef

    state, bundle, prepared, score_log = _prepared(tmp_path, monkeypatch, slow=True)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            accepted = await owner.start(
                prepared.digest,
                target_ref=target,
                secrets={"SCORER_TOKEN": "private-neutral-scorer"},
            )
            ref = ResourceRef("operation", accepted.id)
            async with asyncio.timeout(8):
                while not score_log.exists():
                    await asyncio.sleep(.01)
            with pytest.raises(OwnerError) as busy:
                await owner.start(
                    prepared.digest,
                    target_ref=target,
                    secrets={"SCORER_TOKEN": "private-neutral-scorer"},
                )
            assert busy.value.code == "busy"
            with pytest.raises(OwnerError) as target_busy:
                await owner.stop_target(target)
            assert target_busy.value.code == "busy"
            assert (await owner.stop(ref)).effect == "requested"
            value = await _finished(owner, ref)
            assert value.result.coverage.to_payload() == {"expected": 4, "scored": 1}
            assert value.result.stopped_reason == "cancelled"
            assert value.target_cleanup.complete is True
            assert (await owner.target(target)).state == "ready"

    asyncio.run(scenario())


def test_uncertain_evaluation_settlement_keeps_measurement_and_cleans_target(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner import cleanup as cleanup_module

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    real_read = cleanup_module.read_evaluation_settlement

    def uncertain(*args, **kwargs):
        value = real_read(*args, **kwargs)
        value["ledger"]["uncertain"] += 1
        value["ledger"]["uncertain_any"] = True
        return value

    monkeypatch.setattr(cleanup_module, "read_evaluation_settlement", uncertain)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            accepted = await owner.start(
                prepared.digest,
                target_ref=target,
                secrets={"SCORER_TOKEN": "private-neutral-scorer"},
            )
            ref = ResourceRef("operation", accepted.id)
            value = await _finished(owner, ref)
            assert value.control_state == "cleanup_required", value.to_payload()
            assert value.result.core is not None
            assert value.result.coverage.to_payload() == {"expected": 4, "scored": 4}
            assert value.target_cleanup.complete is None
            outcome = next((state / "scopes").glob("*/evaluation-outcome.json"))
            outcome.unlink()
            assert (await owner.status(ref)).result.stopped_reason == "unknown"
            cleaned = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
            assert cleaned.state == "succeeded", cleaned.to_payload()
            assert (await owner.target(target)).state == "stopped"

    asyncio.run(scenario())


def _watch_finalizing_status(monkeypatch, seen):
    """Read the public status around each finalizing store write of an operation: before every
    ``update_operation`` and after ``mark_operation_cleanup_required``, the window in which a reader
    could see a terminal state whose persistence attempt has not happened yet."""
    from evolution_lab.owner import ResourceRef
    from evolution_lab.owner.api import Owner

    original = Owner._db

    async def watched(self, method, *args, **kwargs):
        if method == "update_operation":
            seen.append(await self.status(ResourceRef("operation", args[0])))
        result = await original(self, method, *args, **kwargs)
        if method == "mark_operation_cleanup_required":
            seen.append(await self.status(ResourceRef("operation", args[0])))
        return result

    monkeypatch.setattr(Owner, "_db", watched)


def test_evaluation_persistence_failure_keeps_cached_public_measurement(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)

    def fail_update(self, *args, **kwargs):
        raise OwnerError("io_error", "finalizing")

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            seen = []
            with monkeypatch.context() as local:
                local.setattr(OwnerStateStore, "update_operation", fail_update)
                _watch_finalizing_status(local, seen)
                accepted = await owner.start(
                    prepared.digest,
                    target_ref=target,
                    secrets={"SCORER_TOKEN": "private-neutral-scorer"},
                )
                ref = ResourceRef("operation", accepted.id)
                value = await _finished(owner, ref)
                assert value.result.core is not None
                assert value.result.persistence.state == "failed"
                assert value.result.persistence.failure.code == "io_error"
                assert value.control_state == "cleanup_required"
            # A terminal state is only ever shown with the outcome of its persistence attempt.
            assert seen
            assert [(v.control_state, v.result.persistence.state) for v in seen
                    if v.control_state not in {"reserved", "running", "stopping"}
                    and v.result is not None and v.result.persistence.state == "not_attempted"] == []
            cleaned = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
            assert cleaned.state == "succeeded", cleaned.to_payload()

    asyncio.run(scenario())


def test_evaluation_recorded_by_the_failure_path_reports_saved(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    original = OwnerStateStore.update_operation
    calls = []

    def fail_first_update(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 1:  # the normal finalization's write; the failure path's write goes through
            raise OwnerError("io_error", "finalizing")
        return original(self, *args, **kwargs)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            with monkeypatch.context() as local:
                local.setattr(OwnerStateStore, "update_operation", fail_first_update)
                accepted = await owner.start(
                    prepared.digest,
                    target_ref=target,
                    secrets={"SCORER_TOKEN": "private-neutral-scorer"},
                )
                ref = ResourceRef("operation", accepted.id)
                await _finished(owner, ref)
            assert len(calls) == 2
            value = await owner.status(ref)
            assert value.control_state == "cleanup_required"
            assert value.result.core is not None
            assert value.result.persistence.state == "saved"
            cleaned = await owner.wait(await owner.cleanup(ref, wait_seconds=8))
            assert cleaned.state == "succeeded", cleaned.to_payload()

    asyncio.run(scenario())


def test_new_core_reader_failure_does_not_fall_back_to_cached_projection(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution.tree
    from evolution_lab.owner import Owner, ResourceRef

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            accepted = await owner.start(
                prepared.digest,
                target_ref=target,
                secrets={"SCORER_TOKEN": "private-neutral-scorer"},
            )
            ref = ResourceRef("operation", accepted.id)
            assert (await _finished(owner, ref)).result.core is not None

            # A new journal tail invalidates the warm projection before the
            # injected fresh-reader failure; its prior core must not return.
            journal = state / "artifacts" / ref.id / ref.id / "measurement" / "events.jsonl"
            with journal.open("ab") as stream:
                stream.write(b"{")

            def unavailable(*args, **kwargs):
                raise ValueError("fixture invalid journal")

            monkeypatch.setattr(evolution.tree, "inspect_journal", unavailable)
            value = await owner.status(ref)
            assert value.results_state == "unavailable"
            assert value.result.core is None
            assert value.result.coverage is None
            assert (await owner.report(ref)).state == "unavailable"
            from evolution_lab.owner import ReportMarkdownResult
            assert await owner.report_markdown(ref) == ReportMarkdownResult(ref.id, "unavailable", None, "unavailable")

    asyncio.run(scenario())


def test_cancelled_start_caller_does_not_cancel_accepted_evaluation(
    tmp_path: Path, monkeypatch
) -> None:
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner.store import OwnerStateStore

    state, bundle, prepared, _ = _prepared(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    real_accept = OwnerStateStore.accept_operation

    def delayed_accept(self, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_accept(self, **kwargs)

    monkeypatch.setattr(OwnerStateStore, "accept_operation", delayed_accept)

    async def scenario():
        async with Owner.open(state) as owner:
            target = await _ready_target(owner, bundle)
            caller = asyncio.create_task(owner.start(
                prepared.digest,
                target_ref=target,
                secrets={"SCORER_TOKEN": "private-neutral-scorer"},
            ))
            assert await asyncio.to_thread(entered.wait, 3)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            async with asyncio.timeout(20):
                while not await owner.list_operations():
                    await asyncio.sleep(.02)
            operation = (await owner.list_operations())[0]
            value = await _finished(owner, ResourceRef("operation", operation.id))
            assert value.control_state == "finished", value.to_payload()
            assert value.result.coverage.to_payload() == {"expected": 4, "scored": 4}

    try:
        asyncio.run(scenario())
    finally:
        release.set()
