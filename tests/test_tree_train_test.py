"""Train/test admission and standalone measurement contracts."""

import json
import os
import shutil

import pytest

from evolution.tree import evolve as evolve_module
from evolution.tree.campaign_measurement import stage_plans
from evolution.tree.evaluation import evaluate, inspect_evaluation
from evolution.tree.evaluation_manifest import load_evaluation_manifest
from evolution.tree.evolve import check_corpus_placement, evolve
from evolution.tree.execution.config import EvaluationPolicy
from evolution.tree.execution.corpus import CorpusError
from evolution.tree.execution.corpus_partition import load_partition
from evolution.tree.execution.interaction import CallRetries, RolloutBudget
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.output_admission import EvolveRefused
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15, load_manifest_v2
from tests.campaign_fakes import FakeAdapter
from tests.test_tree_evolve import served_target as served_target
from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER, v15_from
from tests.tree_evolve_support import lift_document, write_workspace


def rows():
    return [
        {
            "case_id": "train",
            "group_id": "train-group",
            "stratum": "common",
            "oracle": {"critical": True},
            "public": {"text": "hello"},
            "split": "train",
        },
        {
            "case_id": "test",
            "group_id": "test-group",
            "stratum": "common",
            "oracle": {"critical": True},
            "public": {"text": "held-out-canary"},
            "split": "test",
        },
    ]


def evaluation_manifest(tmp_path):
    campaign = write_workspace(tmp_path, rows=rows())
    original = json.loads(campaign.read_text())
    document = {
        name: original[name] for name in ("target", "artifacts", "environment", "scorer", "corpus")
    }
    # PDR-0021: evaluation/1 declares its scalar metric separately from /15's connection contract.
    document["scorer"] = {
        key: value for key, value in document["scorer"].items() if key != "metric_contract"
    }
    document.update(
        schema="evolution-v2-evaluation/1",
        snapshot={"root": "seed"},
        split="test",
        repeats=3,
        random_seed=1,
        max_concurrency=1,
        max_scorer_concurrency=1,
        metric={"primary": "quality"},
        budget={"max_turns": 2, "timeout_seconds": 20, "max_failed_turns": 1},
        sample_retries=0,
        call_retries={"max_attempts": 1, "turn_max_attempts": 1},
    )
    # Match the fixture scorer's metric.
    document["metric"] = original["campaign"]["metric"]
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps(document))
    return path


def test_evaluation_manifest_uses_the_shared_measurement_policy(tmp_path):
    manifest = load_evaluation_manifest(evaluation_manifest(tmp_path))
    assert manifest.policy == EvaluationPolicy(
        3, 1, 1, 1, "score", RolloutBudget(2, 20, 1, CallRetries(1, 1)), 0
    )


def test_evaluation_policy_preserves_one_look_and_canonical_schedule_bytes(tmp_path):
    manifest = load_evaluation_manifest(evaluation_manifest(tmp_path))
    cases = load_partition(manifest.corpus).select(manifest.split).cases
    (plan,) = stage_plans(manifest.policy, cases, "exact", "champion")
    assert (plan.stage, plan.look) == (1, 3)
    assert canonical_json(plan.rows) == (
        b'[{"case_id":"test","repeat":1,"requested_seed":2,"sample_id":"01"},'
        b'{"case_id":"test","repeat":2,"requested_seed":3,"sample_id":"02"},'
        b'{"case_id":"test","repeat":3,"requested_seed":4,"sample_id":"03"}]'
    )


@pytest.mark.parametrize("problem", ["partial", "cross_group", "validation", "duplicate"])
def test_partition_refuses_invalid_labels(tmp_path, problem):
    data = rows()
    if problem == "partial":
        data[1].pop("split")
    elif problem == "cross_group":
        data[1]["group_id"] = data[0]["group_id"]
    elif problem == "validation":
        data[1]["split"] = "validation"
    else:
        data[1]["case_id"] = data[0]["case_id"]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in data))
    with pytest.raises(CorpusError):
        load_partition(path)


async def test_evaluation_test_only_and_completed_resume(tmp_path):
    path = evaluation_manifest(tmp_path)
    adapter = FakeAdapter()

    def factory(*args):
        return adapter

    result = await evaluate(path, tmp_path / "run", adapter_factory=factory)
    assert result["state"] == "completed"
    assert result["completed_samples"] == 3
    assert {row["case_id"] for row in result["measurement_identity"]["schedule"]} == {"test"}
    assert not (tmp_path / "run" / "campaign.jsonl").exists()

    def forbidden(*args):
        raise AssertionError("completed evaluation must not contact Target")

    assert await evaluate(path, tmp_path / "run", adapter_factory=forbidden) == result
    assert inspect_evaluation(tmp_path / "run") == result


async def test_evaluation_resume_refuses_changed_split(tmp_path):
    path = evaluation_manifest(tmp_path)
    await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    document = json.loads(path.read_text())
    document["split"] = "train"
    path.write_text(json.dumps(document))
    with pytest.raises(EvolveRefused, match="run_identity_changed"):
        await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())


async def test_evaluation_cancel_then_resume(tmp_path):
    path = evaluation_manifest(tmp_path)
    result = await evaluate(
        path, tmp_path / "run", cancelled=lambda: True, adapter_factory=lambda *args: FakeAdapter()
    )
    assert result["state"] == "suspended"
    result = await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"


@pytest.mark.parametrize("ledger", ["missing", "empty"])
@pytest.mark.parametrize("torn_owner", [False, True])
def test_evaluation_keeps_an_unborn_store_once_a_measurement_is_bound(
    tmp_path, served_target, ledger, torn_owner
):
    import asyncio
    import subprocess

    from typer.testing import CliRunner

    from evolution.tree.cli import tree_app
    from tests.test_tree_family14_public_links import _fingerprint

    _target, port = served_target
    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["target"]["base_url"] = f"http://127.0.0.1:{port}"
    path.write_text(json.dumps(document))
    run = tmp_path / "run"
    result = asyncio.run(evaluate(path, run, cancelled=lambda: True))
    assert result["state"] == "suspended"
    records = [json.loads(line) for line in (run / "evaluation.jsonl").read_text().splitlines()]
    assert any(record["event"] == "measurement_bound" for record in records)
    artifact_log = run / "artifacts" / "artifacts.jsonl"
    if ledger == "missing":
        artifact_log.unlink()
    else:
        artifact_log.write_bytes(b"")
    if torn_owner:
        owner = run / "evaluation.jsonl"
        owner.write_bytes(owner.read_bytes() + b'{"partial":')
    subprocess.run(["chmod", "-R", "u+w", str(run / "artifacts")], check=True)
    before = _fingerprint(run)
    invocation = CliRunner().invoke(
        tree_app, ["evaluate", "--manifest", str(path), "--run-dir", str(run)]
    )
    assert invocation.exit_code == 2, invocation.output
    assert "refused: artifact_ledger_missing" in invocation.stderr
    assert (run / "artifacts").is_dir()
    assert _fingerprint(run) == before


async def test_evaluation_partial_samples_are_not_repeated(tmp_path):
    from evolution.tree.execution.journal import inspect_journal

    path = evaluation_manifest(tmp_path)
    adapter = FakeAdapter()
    run = tmp_path / "run"

    def stop_after_one():
        location = run / "measurements" / "evaluation"
        if not (location / "events.jsonl").exists():
            return False
        return any(
            record.event == "rollout_finished" for record in inspect_journal(location).records
        )

    first = await evaluate(
        path, run, cancelled=stop_after_one, adapter_factory=lambda *args: adapter
    )
    assert first["state"] == "suspended"
    assert first["completed_samples"] == 1
    second = await evaluate(path, run, adapter_factory=lambda *args: adapter)
    assert second["state"] == "completed"
    records = inspect_journal(run / "measurements" / "evaluation").records
    completed = [
        record.payload["sample_id"] for record in records if record.event == "scoring_finished"
    ]
    assert sorted(completed) == ["01", "02", "03"]


async def test_evaluation_rejects_forged_completion(tmp_path):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    journal = run / "measurements" / "evaluation" / "events.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"broken":')
    with pytest.raises(EvolveRefused, match="evaluation_measurement_invalid"):
        inspect_evaluation(run)


async def test_changed_test_payload_refuses_campaign_resume(tmp_path):
    path = partitioned_manifest(tmp_path)
    run = tmp_path / "run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(corpus.read_text().replace("held-out-canary", "changed-held-out"))
    with pytest.raises(EvolveRefused, match="corpus_partition_changed"):
        await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())


def test_scalar_report_weights_groups_not_samples():
    from types import SimpleNamespace

    from evolution.tree.evaluation import _aggregates

    observations = [
        SimpleNamespace(case_id="a", metrics={"score": 1.0}),
        SimpleNamespace(case_id="b", metrics={"score": 0.0}),
        SimpleNamespace(case_id="c", metrics={"score": 0.0}),
    ]
    configuration = {
        "primary": "score",
        "repeats": 1,
        "case_groups": [["a", "one"], ["b", "two"], ["c", "two"]],
    }
    assert _aggregates(observations, configuration) == {"score": 0.5}


async def test_evaluation_resumes_after_binding_before_child_creation(tmp_path, monkeypatch):
    from evolution.tree.campaign_measurement import Measurement

    path = evaluation_manifest(tmp_path)
    original = Measurement.create

    def crash(*args, **kwargs):
        raise RuntimeError("crash before child creation")

    monkeypatch.setattr(Measurement, "create", crash)
    with pytest.raises(RuntimeError, match="crash before child"):
        await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    monkeypatch.setattr(Measurement, "create", original)
    result = await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"
    records = [
        json.loads(line)
        for line in (tmp_path / "run" / "evaluation.jsonl").read_text().splitlines()
    ]
    assert sum(record["event"] == "measurement_bound" for record in records) == 1


async def test_evaluation_recovers_interrupted_owner_birth(tmp_path):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    (run / "evaluation.lock").touch(mode=0o600)
    (run / "evaluation.jsonl.partial").write_text('{"incomplete":')
    result = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"


def test_evaluation_cli_over_http(tmp_path, served_target):
    from typer.testing import CliRunner

    from evolution.tree.cli import tree_app

    _target, port = served_target
    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["target"]["base_url"] = f"http://127.0.0.1:{port}"
    path.write_text(json.dumps(document))
    invocation = CliRunner().invoke(
        tree_app, ["evaluate", "--manifest", str(path), "--run-dir", str(tmp_path / "run")]
    )
    assert invocation.exit_code == 0, invocation.output
    report = json.loads(invocation.stdout)
    assert report["state"] == "completed"
    assert report["split"] == "test"
    assert "Corpus split train: 1 cases, 1 groups." in invocation.stderr
    assert 'stratum "common": 1 cases, 1 groups.' in invocation.stderr


@pytest.mark.parametrize("torn_source", [False, True])
async def test_evaluation_of_campaign_v0_keeps_source_unchanged(tmp_path, torn_source):
    from tests.test_tree_evolve import fake_adapters

    path = evaluation_manifest(tmp_path)
    source_path = write_workspace(tmp_path / "source")
    source_run = tmp_path / "source" / "run"
    await evolve(source_path, source_run, adapter_factory=fake_adapters())
    if torn_source:
        ledger = source_run / "artifacts.jsonl"
        ledger.write_bytes(ledger.read_bytes() + b'{"kind": "partial')
    before = {
        str(file.relative_to(source_run)): file.read_bytes() for file in source_run.rglob("*.jsonl")
    }
    document = json.loads(path.read_text())
    document["snapshot"] = {"run_dir": "source/run", "version": "v0"}
    path.write_text(json.dumps(document))
    if torn_source:
        with pytest.raises(EvolveRefused, match="evaluation_source_unavailable"):
            await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
        assert not (tmp_path / "run").exists()
    else:
        result = await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
        assert result["state"] == "completed"
        assert result["origin"]["version"] == "v0"
    assert {
        str(file.relative_to(source_run)): file.read_bytes() for file in source_run.rglob("*.jsonl")
    } == before


async def test_evaluation_recovers_interrupted_artifact_birth(tmp_path, monkeypatch):
    from evolution.tree import evaluation

    path = evaluation_manifest(tmp_path)
    original = evaluation._snapshot

    def crash(root, manifest, source, *_floor):
        location = root / "artifacts"
        location.mkdir(mode=0o700)
        (location / "artifacts.jsonl").touch(mode=0o600)
        (location / ".artifacts.lock").touch(mode=0o600)
        raise RuntimeError("artifact birth interrupted")

    monkeypatch.setattr(evaluation, "_snapshot", crash)
    with pytest.raises(RuntimeError, match="artifact birth interrupted"):
        await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    monkeypatch.setattr(evaluation, "_snapshot", original)
    result = await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"


def partitioned_manifest(tmp_path, *, schema=SCHEMA_V15):
    """The labelled workspace with `v11_document`'s campaign, declared `schema`:
    `/15` (family 14) by default."""

    from tests.tree.manifest.test_manifest_v2 import v11_document

    path = write_workspace(tmp_path, rows=rows())
    document = json.loads(path.read_text())
    document["campaign"] = v11_document()["campaign"]
    document["reflector"]["max_consecutive_failures"] = 2
    path.write_text(json.dumps(lift_document(document, schema)))
    return path


def write_corpus(tmp_path, data):
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in data))
    return path


@pytest.mark.parametrize("kind", ["unlabeled", "train", "test"])
async def test_single_split_corpus_can_be_evaluated(tmp_path, kind):
    path = evaluation_manifest(tmp_path)
    data = rows()[:1]
    if kind == "unlabeled":
        data[0].pop("split")
    else:
        data[0]["split"] = kind
    partition = load_partition(write_corpus(tmp_path, data))
    selected = "test" if kind == "test" else "train"
    assert len(partition.select(selected).cases) == 1
    with pytest.raises(CorpusError, match="empty_split"):
        partition.select("train" if selected == "test" else "test")
    document = json.loads(path.read_text())
    document["split"] = selected
    path.write_text(json.dumps(document))
    result = await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"


@pytest.mark.parametrize("kind", ["unlabeled", "train", "test"])
async def test_campaign_requires_only_a_nonempty_train_split(tmp_path, kind):
    path = partitioned_manifest(tmp_path)
    data = rows()[:1]
    if kind == "unlabeled":
        data[0].pop("split")
    else:
        data[0]["split"] = kind
    write_corpus(tmp_path, data)
    if kind == "test":
        with pytest.raises(EvolveRefused, match="empty_split"):
            await evolve(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
    else:
        result = await evolve(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())
        assert result.terminal == "finished"


@pytest.mark.parametrize("completed", [False, True])
async def test_evaluation_refuses_corrupt_artifact_ledger(tmp_path, completed):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    await evaluate(
        path, run, cancelled=lambda: not completed, adapter_factory=lambda *args: FakeAdapter()
    )
    ledger = run / "artifacts" / "artifacts.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b'{"broken": 1}\n')
    with pytest.raises(EvolveRefused, match="evaluation_snapshot_invalid"):
        inspect_evaluation(run)
    before = ledger.read_bytes()
    with pytest.raises(EvolveRefused, match="evaluation_snapshot_invalid"):
        await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("completed", [False, True])
async def test_evaluation_resumes_after_directory_move(tmp_path, completed):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    first = await evaluate(
        path, run, cancelled=lambda: not completed, adapter_factory=lambda *args: FakeAdapter()
    )
    moved = tmp_path / "moved"
    run.rename(moved)
    result = await evaluate(path, moved, adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"
    assert result["run_id"] == first["run_id"]


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "snapshot",
        "corpus",
        "split",
        "target",
        "artifacts",
        "environment",
        "scorer",
        "repeats",
        "random_seed",
        "max_concurrency",
        "max_scorer_concurrency",
        "metric",
        "budget",
        "sample_retries",
        "call_retries",
    ],
)
def test_evaluation_manifest_requires_every_declared_key(tmp_path, field):
    from evolution.tree.evaluation_manifest import load_evaluation_manifest
    from evolution.tree.execution.config import ManifestError

    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    del document[field]
    path.write_text(json.dumps(document))
    with pytest.raises(ManifestError):
        load_evaluation_manifest(path)


@pytest.mark.parametrize("field", ["rounds", "editor", "linked_from", "unknown"])
def test_evaluation_manifest_refuses_unknown_keys(tmp_path, field):
    from evolution.tree.evaluation_manifest import load_evaluation_manifest
    from evolution.tree.execution.config import ManifestError

    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document[field] = {}
    path.write_text(json.dumps(document))
    with pytest.raises(ManifestError, match="unknown_key"):
        load_evaluation_manifest(path)


@pytest.mark.parametrize("changed", ["snapshot", "scorer"])
async def test_evaluation_refuses_changed_bound_content(tmp_path, changed):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    file = tmp_path / ("seed/skills/SKILL.md" if changed == "snapshot" else "scorer/score.py")
    file.write_text(file.read_text() + "\n# changed\n")
    with pytest.raises(EvolveRefused, match="run_identity_changed"):
        await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())


@pytest.mark.parametrize("role", ["editor", "reflector", "grouping"])
@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
async def test_corpus_cannot_be_a_role_prompt(tmp_path, role, alias, monkeypatch):
    path = partitioned_manifest(tmp_path)
    document = json.loads(path.read_text())
    corpus = tmp_path / "corpus.jsonl"
    prompt = tmp_path / "prompt.txt"
    if alias == "symlink":
        prompt.symlink_to(corpus)
    elif alias == "hardlink":
        prompt.hardlink_to(corpus)
    else:
        prompt = corpus
    from tests.tree.manifest.test_manifest_v2 import RETRY

    section = {
        "kind": "chat",
        "model": "test",
        "base_url": "http://127.0.0.1:9999/v1",
        "api_key_env": "TEST_CHAT_KEY",
        "system_prompt": str(prompt),
        "retry": RETRY,
    }
    if role == "reflector":
        section = {
            **{
                key: value
                for key, value in document[role].items()
                if key not in {"kind", "argv", "cwd", "dependency_paths", "allowed_env"}
            },
            **section,
        }
    if role == "grouping":
        from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER

        document["grouper"] = {**CHAT_GROUPER, **section}
    else:
        document[role] = section
    path.write_text(json.dumps(lift_document(document, SCHEMA_V15)))
    monkeypatch.setenv("TEST_CHAT_KEY", "test-key")
    with pytest.raises(EvolveRefused, match="corpus_inside_role_workspace"):
        await evolve(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())


# -- check_corpus_placement (#234) ----------------------------------------------

PLACEMENT_ENV = {"TEST_CHAT_KEY": "test-key", "GROUPER_API_KEY": "grouper-key"}
#: Layouts on which `evolve` refuses with `corpus_inside_role_workspace` first
#: (the equivalence test proves it). `run_dir` is the helper's only: `evolve`
#: refuses a run directory holding a foreign file earlier, as `run_dir_not_campaign`.
REFUSED_LAYOUTS = (
    "seed",
    "scorer",
    "editor",
    "reflector",
    "editor_prompt",
    "grouper_prompt",
    "grouper_prompt_symlink",
    "grouper_prompt_hardlink",
)


def placement_workspace(tmp_path, layout):
    """A `/15` workspace (`write_workspace` lifted by `v15_from`) whose corpus
    lies where `layout` says; `clean` keeps it beside the manifest."""

    from tests.tree.manifest.test_manifest_v2 import RETRY

    path = write_workspace(tmp_path)
    document = v15_from(json.loads(path.read_text()))
    corpus = tmp_path / "corpus.jsonl"
    if layout in ("seed", "scorer", "editor", "reflector", "run_dir"):
        directory = tmp_path / ("run" if layout == "run_dir" else layout)
        directory.mkdir(exist_ok=True)
        shutil.move(corpus, directory / "corpus.jsonl")
        document["corpus"]["path"] = f"{directory.name}/corpus.jsonl"
    elif layout == "editor_prompt":
        document["editor"] = {
            "kind": "chat",
            "model": "test",
            "base_url": "http://127.0.0.1:9999/v1",
            "api_key_env": "TEST_CHAT_KEY",
            "system_prompt": "corpus.jsonl",
            "retry": RETRY,
            "context_compact_chars": 800000,
            "context_message_max_chars": 64000,
        }
    elif layout.startswith("grouper_prompt"):
        prompt = "corpus.jsonl"
        if layout != "grouper_prompt":
            alias = tmp_path / "prompt.txt"
            if layout.endswith("symlink"):
                alias.symlink_to(corpus)
            else:
                alias.hardlink_to(corpus)
            prompt = alias.name
        document["grouper"] = {**CHAT_GROUPER, "system_prompt": prompt}
    else:
        assert layout == "clean"
    path.write_text(json.dumps(document))
    return path


@pytest.mark.parametrize("layout", [*REFUSED_LAYOUTS, "run_dir"])
def test_check_corpus_placement_refuses_a_corpus_a_role_can_read(tmp_path, layout):
    path = placement_workspace(tmp_path, layout)
    with pytest.raises(EvolveRefused) as caught:
        check_corpus_placement(
            path, run_root=tmp_path / "run", environ={**os.environ, **PLACEMENT_ENV}
        )
    assert (caught.value.code, str(caught.value)) == (
        "corpus_inside_role_workspace",
        "corpus_inside_role_workspace",
    )
    assert not (tmp_path / "run" / "campaign.jsonl").exists()


def test_check_corpus_placement_accepts_a_clean_v15_layout_and_writes_nothing(tmp_path):
    path = placement_workspace(tmp_path, "clean")
    environ = {**os.environ, **PLACEMENT_ENV}
    before = sorted(entry.relative_to(tmp_path) for entry in tmp_path.rglob("*"))
    assert check_corpus_placement(path, run_root=tmp_path / "run", environ=environ) is None
    # An already loaded document gives the same answer.
    manifest = load_manifest_v2(path)
    assert check_corpus_placement(manifest, run_root=tmp_path / "run", environ=environ) is None
    assert sorted(entry.relative_to(tmp_path) for entry in tmp_path.rglob("*")) == before
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("role", ["editor", "grouper"])
def test_check_corpus_placement_never_resolves_an_undeclared_cli(tmp_path, role):
    """#248, option A: an `agent_cli` role without `agent_bin` is not looked up
    on `PATH`, so the helper never reports `editor_program_missing` (or the
    Grouper's code) for a machine that is not the launch machine."""

    path = write_workspace(tmp_path)
    document = v15_from(json.loads(path.read_text()))
    if role == "editor":
        document["editor"] = {"kind": "agent_cli", "model": "codex"}
    else:
        from tests.tree.manifest.test_manifest_v15 import AGENT_GROUPER

        document["grouper"] = dict(AGENT_GROUPER)
    path.write_text(json.dumps(document))
    empty = tmp_path / "empty-path"
    empty.mkdir()
    assert (
        check_corpus_placement(path, run_root=tmp_path / "run", environ={"PATH": str(empty)})
        is None
    )
    assert check_corpus_placement(path) is None


@pytest.mark.parametrize("layout", REFUSED_LAYOUTS)
def test_check_corpus_placement_without_a_run_root_still_checks_declared_paths(tmp_path, layout):
    path = placement_workspace(tmp_path, layout)
    with pytest.raises(EvolveRefused) as caught:
        check_corpus_placement(path, environ={**os.environ, **PLACEMENT_ENV})
    assert caught.value.code == "corpus_inside_role_workspace"


def test_check_corpus_placement_without_a_run_root_skips_the_run_directory(tmp_path):
    path = placement_workspace(tmp_path, "run_dir")
    assert check_corpus_placement(path, run_root=None) is None
    with pytest.raises(EvolveRefused, match="corpus_inside_role_workspace"):
        check_corpus_placement(path, run_root=tmp_path / "run")


async def test_evolve_also_keeps_the_corpus_apart_from_the_path_resolved_cli(tmp_path, monkeypatch):
    """The one difference option A leaves: the corpus *is* the agent CLI that
    `evolve` resolves on the bound `PATH` (compared with `samefile`)."""

    path = write_workspace(tmp_path)
    document = v15_from(json.loads(path.read_text()))
    document["editor"] = {"kind": "agent_cli", "model": "codex"}
    path.write_text(json.dumps(document))
    corpus = tmp_path / "corpus.jsonl"
    corpus.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").symlink_to(corpus)
    environ = {**os.environ, **PLACEMENT_ENV, "PATH": str(bindir)}
    assert check_corpus_placement(path, run_root=tmp_path / "run", environ=environ) is None
    with pytest.raises(EvolveRefused) as caught:
        await evolve(
            path, tmp_path / "run", adapter_factory=lambda *a, **k: object(), environ=environ
        )
    assert caught.value.code == "corpus_inside_role_workspace"


@pytest.mark.parametrize("layout", [*REFUSED_LAYOUTS, "clean"])
async def test_check_corpus_placement_agrees_with_evolve(tmp_path, monkeypatch, layout):
    path = placement_workspace(tmp_path, layout)
    environ = {**os.environ, **PLACEMENT_ENV}

    class Reached(Exception):
        """`evolve` passed every admission step, the corpus rule included."""

    async def entry(policy, **kwargs):
        raise Reached

    monkeypatch.setattr(evolve_module, "run_campaign", entry)
    try:
        check_corpus_placement(path, run_root=tmp_path / "run", environ=environ)
    except EvolveRefused as exc:
        helper = exc.code
    else:
        helper = None
    try:
        await evolve(
            path, tmp_path / "run", adapter_factory=lambda *a, **k: object(), environ=environ
        )
    except Reached:
        evolved = None
    except EvolveRefused as exc:
        evolved = exc.code
    else:
        pytest.fail("evolve returned without reaching run_campaign")
    expected = None if layout == "clean" else "corpus_inside_role_workspace"
    assert helper == evolved == expected


def test_public_split_does_not_override_assignment(tmp_path):
    data = rows()
    data[0]["public"]["split"] = "test"
    data[1]["public"]["split"] = "train"
    partition = load_partition(write_corpus(tmp_path, data))
    assert [case.case_id for case in partition.train.cases] == ["train"]
    assert [case.case_id for case in partition.test.cases] == ["test"]


def test_optional_metrics_report_their_own_coverage():
    from types import SimpleNamespace

    from evolution.tree.evaluation import _aggregates, _metric_coverage

    data = [
        SimpleNamespace(case_id="a", metrics={"score": 1.0, "extra": 0.2}),
        SimpleNamespace(case_id="b", metrics={"score": 0.0}),
    ]
    config = {"primary": "score", "repeats": 1, "case_groups": [["a", "g1"], ["b", "g2"]]}
    assert _aggregates(data, config) == {"score": 0.5, "extra": 0.2}
    assert _metric_coverage(data, config)["extra"] == {"samples": 1, "cases": 1, "groups": 1}


@pytest.mark.parametrize("fault", ["target_failed", "infrastructure_failed", "time_budget"])
@pytest.mark.parametrize("retries", [0, 1])
async def test_each_explicit_resume_repairs_one_exhausted_sample(
    tmp_path, monkeypatch, fault, retries
):
    from evolution.tree.campaign_measurement import Measurement
    from tests.campaign_fakes import FakeInstance

    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document.update(repeats=1, sample_retries=retries)
    path.write_text(json.dumps(document))
    current = fault
    monkeypatch.setattr(FakeInstance, "_stored", lambda self, session: current)
    run = tmp_path / "run"
    ordinals = []
    for _ in range(3):
        report = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
        assert report["state"] == "suspended"
        (row,) = Measurement.open_frozen(run, "evaluation").sample_rows(1)
        ordinals.append(row.retry_ordinal)
    assert ordinals[1:] == [ordinals[0] + 1, ordinals[0] + 2]
    current = "completed"
    report = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert report["state"] == "completed"


async def test_executed_unscored_sample_gets_a_full_repair_try(tmp_path):
    from evolution.tree.execution.journal import inspect_journal

    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["repeats"] = 1
    path.write_text(json.dumps(document))
    marker = tmp_path / "scorer-unavailable"
    marker.touch()
    script = tmp_path / "scorer" / "score.py"
    script.write_text(
        "import pathlib, sys\nif pathlib.Path("
        + repr(str(marker))
        + ").exists(): sys.exit(1)\n"
        + script.read_text()
    )
    run = tmp_path / "run"
    first = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert first["state"] == "suspended"
    records = inspect_journal(run / "measurements" / "evaluation").records
    assert any(row.event == "scoring_failed" for row in records)
    marker.unlink()
    second = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert second["state"] == "completed"
    records = inspect_journal(run / "measurements" / "evaluation").records
    assert sum(row.event == "rollout_started" for row in records) == 2


async def test_scored_candidate_failure_is_not_retried(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.campaign_fakes import FakeInstance

    original = FakeInstance.send

    async def candidate_failure(self, *args):
        return replace(await original(self, *args), outcome="candidate_failed")

    monkeypatch.setattr(FakeInstance, "send", candidate_failure)
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    first = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert first["state"] == "completed"

    def forbidden(*args):
        raise AssertionError("scored failure must not contact the target again")

    assert await evaluate(path, run, adapter_factory=forbidden) == first


def test_changed_partition_is_a_hard_cli_refusal(tmp_path, served_target):
    from typer.testing import CliRunner

    from evolution.tree.cli import tree_app

    _, port = served_target
    path = partitioned_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["target"]["base_url"] = f"http://127.0.0.1:{port}"
    path.write_text(json.dumps(document))
    command = ["evolve", "--manifest", str(path), "--run-dir", str(tmp_path / "run")]
    first = CliRunner().invoke(tree_app, command)
    assert first.exit_code == 0, first.output
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(corpus.read_text().replace("held-out-canary", "changed-held-out"))
    result = CliRunner().invoke(tree_app, command)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "corpus_partition_changed" in result.stderr
    assert "class: hard" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("evaluation_seed", [1, 999])
async def test_train_evaluation_matches_campaign_baseline(tmp_path, monkeypatch, evaluation_seed):
    from dataclasses import replace

    from evolution.tree.execution.journal import inspect_journal
    from tests.campaign_fakes import FakeInstance

    original = FakeInstance.send

    async def seed_score(self, session, message, request_id):
        result = await original(self, session, message, request_id)
        return replace(
            result, events=({"type": "score", "value": session.requested_seed % 10 / 10},)
        )

    monkeypatch.setattr(FakeInstance, "send", seed_score)
    path = evaluation_manifest(tmp_path)
    campaign = partitioned_manifest(tmp_path)
    document = json.loads(campaign.read_text())
    document["campaign"]["random_seed"] = 1
    document["campaign"]["evaluation"] = {"repeats_per_question": 3}
    campaign.write_text(json.dumps(document))
    (tmp_path / "scorer" / "score.py").write_text(
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "score = request['view']['history']['turns'][0]['events'][0]['value']\n"
        "print(json.dumps({'metrics': {'score': score}, 'critical_pass': True, "
        "'verdicts': [{'criterion': 'answered', 'passed': False}]}))\n"
    )
    await evolve(campaign, tmp_path / "campaign-run", adapter_factory=lambda *args: FakeAdapter())
    evaluation = json.loads(path.read_text())
    evaluation.update(
        split="train", random_seed=evaluation_seed, budget=document["campaign"]["budget"]
    )
    path.write_text(json.dumps(evaluation))
    result = await evaluate(
        path, tmp_path / "evaluation-run", adapter_factory=lambda *args: FakeAdapter()
    )
    baseline = inspect_journal(tmp_path / "campaign-run" / "measurements" / "m-r00-champion")
    identity = baseline.records[0].payload["identity"]
    measured = result["measurement_identity"]
    for key in ("candidate_digest", "target", "environment", "scorer", "corpus_digest", "budget"):
        assert measured[key] == identity[key]

    def schedule(value):
        return [(row["case_id"], row["repeat"], row["requested_seed"]) for row in value["schedule"]]

    assert schedule(identity) == [("train", 1, 2), ("train", 2, 3), ("train", 3, 4)]
    assert (schedule(measured) == schedule(identity)) == (evaluation_seed == 1)
    assert result["metrics"]["score"] == pytest.approx(0.3 if evaluation_seed == 1 else 0.1)


def test_partition_preflight_reports_group_counts_and_missing_strata(tmp_path):
    from evolution.tree.execution.corpus_partition import partition_notes

    data = rows()
    data[0]["stratum"] = "train-only"
    data[1]["stratum"] = "test-only"
    data.append({**data[0], "case_id": "train2"})
    notes = partition_notes(load_partition(write_corpus(tmp_path, data)))
    assert "Corpus split train: 2 cases, 1 groups." in notes
    assert 'Corpus split train, stratum "train-only": 2 cases, 1 groups.' in notes
    assert 'Corpus split coverage: stratum "train-only" is absent from test.' in notes
    assert 'Corpus split coverage: stratum "test-only" is absent from train.' in notes


def test_partition_preflight_identifies_implicit_train(tmp_path):
    from evolution.tree.execution.corpus_partition import partition_notes

    data = rows()[:1]
    data[0].pop("split")
    assert "Corpus partition_mode: implicit_train." in partition_notes(
        load_partition(write_corpus(tmp_path, data))
    )


@pytest.mark.parametrize(
    "journal",
    ["evaluation.jsonl", "artifacts/artifacts.jsonl", "measurements/evaluation/events.jsonl"],
)
async def test_evaluation_recovers_torn_append_without_repeating_scored_samples(tmp_path, journal):
    from evolution.tree.execution.journal import inspect_journal

    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    measurement = run / "measurements" / "evaluation"

    def stop_after_one():
        return (measurement / "events.jsonl").exists() and any(
            row.event == "rollout_finished" for row in inspect_journal(measurement).records
        )

    first = await evaluate(
        path, run, cancelled=stop_after_one, adapter_factory=lambda *args: FakeAdapter()
    )
    assert first["completed_samples"] == 1
    log = run / journal
    prefix = log.read_bytes()
    torn = prefix + b'{"kind": "partial'
    log.write_bytes(torn)
    with pytest.raises(EvolveRefused):
        inspect_evaluation(run)
    assert log.read_bytes() == torn
    second = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert (second["state"], second["completed_samples"], second["run_id"]) == (
        "completed",
        3,
        first["run_id"],
    )
    assert log.read_bytes().startswith(prefix)
    records = inspect_journal(measurement).records
    for event in ("rollout_started", "scoring_finished"):
        samples = [row.payload["sample_id"] for row in records if row.event == event]
        assert samples.count("01") == 1
        if event == "scoring_finished":
            assert sorted(samples) == ["01", "02", "03"]
    assert await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter()) == second


async def test_train_boundary_reaches_editor_gate_feedback_and_resumed_history(
    tmp_path, monkeypatch
):
    from evolution.tree.roles.editor.subprocess_editor import SubprocessEditor
    from evolution.tree.roles.editor_contracts import brief_payload
    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload
    from evolution.tree.roles.reflector.subprocess_reflector import SubprocessReflector
    from evolution.tree.roles.reflector_contracts import ReflectResult
    from tests.tree_evolve_support import REJECTING_SCORER_SCRIPT

    path = partitioned_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["campaign"].update(rounds=2, attempts_per_round=2, parallel_attempts=1)
    document["reflector"]["families"] = {"kind": "metadata", "path": "/family"}
    path.write_text(json.dumps(document))
    data = rows()
    for row in data:
        row["reflection_metadata"] = {"family": "shared"}
    data[1]["oracle"]["private"] = "held-out-oracle-canary"
    data[1]["reflection_metadata"]["note"] = "held-out-metadata-canary"
    write_corpus(tmp_path, data)
    (tmp_path / "scorer" / "score.py").write_text(REJECTING_SCORER_SCRIPT)
    diagnosis = {
        "status": "reflected",
        "note": "train diagnosis",
        "groups": [
            {
                "root_cause": "skill misses the answer shape",
                "suggested_fix": "state the answer shape",
                "evidence_ids": ["obs:train:1"],
                "affected_cases": ["train"],
                "target_paths": ["skills/SKILL.md"],
                "counterevidence": "no passing observation",
                "regression_risks": "preserve the response format",
                "why_this_layer": "editable skill controls the answer",
            }
        ],
    }
    (tmp_path / "reflector" / "reflect.py").write_text(
        "print(" + repr(json.dumps(diagnosis)) + ")\n"
    )
    reflected, edited, tool_results = [], [], []
    original_reflect, original_edit = SubprocessReflector.reflect, SubprocessEditor.edit
    suspended = False

    async def capture_reflection(self, request):
        nonlocal suspended
        reflected.append((request.round, reflect_brief_payload(request.evidence)))
        catalog = request.reader.dispatch("list_evidence", {})
        assert catalog["total"] == 1
        family = catalog["entries"][0]["family_id"]
        members = request.reader.dispatch("list_evidence", {"family_id": family})
        assert members["total"] == 1
        tool_results.extend((catalog, members))
        for repeat in (1, 2, 3):
            evidence = request.reader.dispatch(
                "read_evidence", {"evidence_id": f"obs:train:{repeat}"}
            )
            assert evidence["next_offset"] is None
            tool_results.append(evidence)
        if request.round == 2 and not suspended:
            suspended = True
            return ReflectResult("failed", (), "provider unavailable", code="provider_exhausted")
        return await original_reflect(self, request)

    async def capture_edit(self, request):
        edited.append(brief_payload(request.brief))
        return await original_edit(self, request)

    monkeypatch.setattr(SubprocessReflector, "reflect", capture_reflection)
    monkeypatch.setattr(SubprocessEditor, "edit", capture_edit)
    run = tmp_path / "run"
    first = await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert first.terminal == "suspended"
    assert first.suspended["owner"] == "reflector"
    second = await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert second.terminal == "finished"
    journal = [json.loads(line) for line in (run / "campaign.jsonl").read_text().splitlines()]
    scopes = [row["payload"]["cases"] for row in journal if row["event"] == "gate_scope_selected"]
    assert scopes and all(scope == ["train"] for scope in scopes)
    assert journal[0]["payload"]["adaptation_payload"]["family_membership"] == [["train", "shared"]]
    assert sum(row["event"] == "attempt_sealed" for row in journal) == 4
    assert all(
        attempt["outcome"] == "rejected"
        for round_ in second.result.to_payload()["rounds"]
        for attempt in round_["attempts"]
    )
    assert [round_ for round_, _ in reflected] == [1, 2, 2]
    assert reflected[-1][1]["history"]["rounds"]
    assert len(edited) == 4
    assert edited[1]["attempt_feedback"]
    exposed = json.dumps({"reflector": reflected, "editor": edited, "tools": tool_results})
    for marker in (
        "held-out-canary",
        "held-out-oracle-canary",
        "held-out-metadata-canary",
        '"test"',
    ):
        assert marker not in exposed
    from evolution.tree.execution.journal import inspect_journal

    for measurement in (run / "measurements").iterdir():
        schedule = inspect_journal(measurement).records[0].payload["identity"]["schedule"]
        assert {row["case_id"] for row in schedule} == {"train"}


@pytest.mark.parametrize("journal", ["evaluation.jsonl", "artifacts/artifacts.jsonl"])
async def test_corrupt_complete_record_is_not_repaired(tmp_path, journal):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    await evaluate(path, run, cancelled=lambda: True, adapter_factory=lambda *args: FakeAdapter())
    log = run / journal
    damaged = log.read_bytes() + b'{"kind": "invalid"}\n'
    log.write_bytes(damaged)

    def forbidden(*args):
        raise AssertionError("corrupt owner or artifact ledger must refuse before Target access")

    with pytest.raises(EvolveRefused):
        await evaluate(path, run, adapter_factory=forbidden)
    assert log.read_bytes() == damaged


@pytest.mark.parametrize("ledger", ["present", "missing", "empty"])
async def test_changed_identity_does_not_repair_owner_tail(tmp_path, ledger):
    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    await evaluate(path, run, cancelled=lambda: True, adapter_factory=lambda *args: FakeAdapter())
    log = run / "evaluation.jsonl"
    damaged = log.read_bytes() + b'{"kind": "partial'
    log.write_bytes(damaged)
    document = json.loads(path.read_text())
    document["random_seed"] += 1
    path.write_text(json.dumps(document))
    artifact_log = run / "artifacts" / "artifacts.jsonl"
    if ledger == "missing":
        artifact_log.unlink()
    elif ledger == "empty":
        artifact_log.write_bytes(b"")
    with pytest.raises(EvolveRefused, match="run_identity_changed"):
        await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert log.read_bytes() == damaged


@pytest.mark.parametrize("ledger", ["missing", "empty"])
async def test_completed_evaluation_keeps_verification_precedence_over_a_lost_ledger(
    tmp_path, ledger
):
    from evolution.tree.storage.journal_errors import JournalFault
    from tests.test_tree_family14_public_links import _fingerprint

    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    result = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"
    artifact_log = run / "artifacts" / "artifacts.jsonl"
    if ledger == "missing":
        artifact_log.unlink()
    else:
        artifact_log.write_bytes(b"")
    before = _fingerprint(run)

    def forbidden(*args):
        pytest.fail("completed evaluation must verify without contacting the target")

    error = JournalFault if ledger == "missing" else EvolveRefused
    code = "journal_unavailable" if ledger == "missing" else "evaluation_snapshot_invalid"
    with pytest.raises(error, match=code):
        await evaluate(path, run, adapter_factory=forbidden)
    assert _fingerprint(run) == before


async def test_established_owner_without_valid_creation_record_is_not_repaired(tmp_path):
    from evolution.tree.execution.journal import JournalFault

    path = evaluation_manifest(tmp_path)
    run = tmp_path / "run"
    await evaluate(path, run, cancelled=lambda: True, adapter_factory=lambda *args: FakeAdapter())
    log = run / "evaluation.jsonl"
    damaged = b'{"kind": "partial'
    log.write_bytes(damaged)
    with pytest.raises(JournalFault):
        await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert log.read_bytes() == damaged
