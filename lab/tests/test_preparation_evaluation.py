"""S3 Task 1: immutable fixed-evaluation preparation."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from evolution_lab.preparation.internal_manifest import load_internal_manifest
from scaffold_15 import scaffold_15


def _descriptor():
    from evolution_lab.preparation.models import EnvironmentDescriptor

    return EnvironmentDescriptor.from_payload({
        "schema": "lab-environment/1",
        "executable": sys.executable,
        "executable_sha256": "a" * 64,
        "python_version": "3.12.13",
        "distributions": [
            {"name": "evolution-lab", "version": "0.0.3.dev8", "wheel_sha256": "b" * 64},
            {"name": "evo-skills", "version": "0.3.0a8", "wheel_sha256": "c" * 64},
        ],
        "runtime_files": [],
        "platform": {"system": "Linux", "machine": "x86_64"},
        "proof_profile": "wheel-members/1",
        "limitations": ["synthetic capture boundary"],
    })


def _evaluation_request(
    tmp_path: Path, monkeypatch, *, reserved: bool = False,
    schema: str = "lab-evaluation-input/1", extra_defaults: dict | None = None,
):
    from evolution_lab.preparation import EvaluationSource, EnvironmentInput, SourceRoot
    from evolution_lab.preparation.environment import CapturedEnvironment
    import evolution_lab.preparation.bundle as bundle_module

    root = tmp_path / "evaluation-source"
    scaffold_15(root)
    descriptor = _descriptor()
    monkeypatch.setattr(
        bundle_module,
        "capture_environment",
        lambda value, *, execution: CapturedEnvironment(descriptor, '{"archive_wheels":[]}'),
    )
    corpus = root / "evaluation.jsonl"
    rows = [
        {"case_id": "c-2", "group_id": "g", "stratum": "s", "public": {"text": "two"}},
        {"case_id": "c-1", "group_id": "g", "stratum": "s", "public": {"text": "one"}},
        {"case_id": "c-3", "group_id": "g", "stratum": "s", "public": {"text": "three"}},
    ]
    if reserved:
        rows[-1]["public"]["conformance"] = True
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))
    scorer = root / "scorer"
    scorer.mkdir(exist_ok=True)
    program = scorer / "score.py"
    program.write_text("from pathlib import Path\nPath('SCORER_EXECUTED').write_text('unexpected')\n")
    dependency = scorer / "rubric.json"
    dependency.write_text("{}")
    raw_campaign = json.loads((root / "manifest.json").read_text())
    evaluation_input = root / "evaluation-input.json"
    evaluation_input.write_text(json.dumps({
        "schema": schema,
        "seed_root": "seed",
        "policy": {"version": 1, **raw_campaign["artifacts"]},
        "target": raw_campaign["target"],
        "corpus": {"path": "evaluation.jsonl"},
        "environment": {"kind": "static", "input_text_key": "text"},
        "scorer": {
            "kind": "subprocess",
            "argv": [sys.executable, "score.py"],
            "cwd": "scorer",
            "dependency_paths": ["scorer/rubric.json"],
            "allowed_env": ["SCORER_TOKEN", "MODE"],
            "timeout_seconds": 7,
        },
        "evaluation_set_id": "fixed-set",
        "metric_plan": {"primary": "score", "weights": [1, 0.5]},
        "defaults": {
            "repeat_seeds": [17, 17], "max_turns": 4, "timeout_seconds": 9,
            **(extra_defaults or {}),
        },
    }))
    launch = root / "launch.json"
    launch.write_text(json.dumps({
        "schema": "lab-target-launch/1",
        "command": {"executable": sys.executable, "args": [{"bundle_path": "launcher.py"}]},
        "environment_ref": descriptor.digest,
        "program_paths": ["launcher.py"],
        "env": {"MODE": "offline"},
        "secret_names": ["TARGET_TOKEN"],
        "endpoint": {"base_url": raw_campaign["target"]["base_url"]},
        "identity": {
            "target_id": "neutral-target",
            "contract_digest": None,
            "settings_hash": None,
            "runtime_ref_digest": None,
            "seed_mode": "exact",
        },
        "policy": "evaluation-input.json",
        "limits": {
            "max_instances": 1,
            "max_concurrent_sessions": 1,
            "max_input_bytes": 1 << 20,
            "max_output_bytes": 1 << 20,
        },
        "startup_timeout_seconds": 10,
        "stop_grace_seconds": 10,
        "conformance": None,
    }))
    launcher = root / "launcher.py"
    launcher.write_text("from pathlib import Path\nPath('TARGET_EXECUTED').write_text('unexpected')\n")
    launcher.chmod(0o755)
    return {
        "source": EvaluationSource(evaluation_input, launch),
        "environments": (EnvironmentInput(Path(sys.executable), (tmp_path / "retained.whl",)),),
        "env": {"MODE": "offline"},
        "secret_names": ("SCORER_TOKEN", "TARGET_TOKEN"),
        "roots": (SourceRoot("project", root),),
    }


def _evaluation_bundle(
    tmp_path: Path, monkeypatch, *, reserved: bool = False,
    schema: str = "lab-evaluation-input/1", extra_defaults: dict | None = None,
):
    from evolution_lab.preparation import PreparationStore

    request = _evaluation_request(
        tmp_path, monkeypatch, reserved=reserved, schema=schema, extra_defaults=extra_defaults,
    )
    state = tmp_path / "state"
    store = PreparationStore(state)
    bundle = store.create_bundle(**request)
    return bundle, state


def _campaign_bundle(tmp_path: Path, monkeypatch):
    from test_preparation_api import _request
    from evolution_lab.preparation import PreparationStore

    request = _request(tmp_path, monkeypatch)
    source_root = request["source"].manifest_path.parent
    corpus = source_root / "corpus.jsonl"
    rows = [json.loads(line) for line in corpus.read_text().splitlines()]
    rows[0]["reflection_metadata"] = {"failure_family": "format", "rank": 2}
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))
    state = tmp_path / "state"
    store = PreparationStore(state)
    campaign = store.create_bundle(**request)
    saved_corpus = store._storage.get_bundle(campaign.digest).path / "inputs/project/corpus.jsonl"
    assert "reflection_metadata" in saved_corpus.read_text()
    shutil.rmtree(source_root)
    return campaign, state


def test_evaluation_bundle_and_prepared_reopen_from_saved_bytes_without_execution(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    source_root = request["source"].input_path.parent
    state = tmp_path / "state"
    store = PreparationStore(state)

    bundle = store.create_bundle(**request)
    stored = store._storage.get_bundle(bundle.digest)
    assert bundle.source_kind == "evaluation"
    assert stored.descriptor.entrypoint != stored.descriptor.conformance_entrypoint
    manifest = load_internal_manifest(stored.path / stored.descriptor.conformance_entrypoint)
    assert manifest.schema == "evolution-v2-manifest/1"
    assert manifest.workflow is None
    launch = json.loads((stored.path / stored.descriptor.launch).read_text())
    assert launch["policy"] == stored.descriptor.conformance_entrypoint
    shutil.rmtree(source_root)

    prepared = PreparationStore(state).prepare_evaluation(
        bundle.digest, selection={"case_ids": ["c-3", "c-2"]}
    )
    assert prepared.to_payload() == {
        "schema": "lab-prepared-summary/1",
        "digest": prepared.digest,
        "bundle_digest": bundle.digest,
        "kind": "evaluation",
        "target_binding": {
            "launch_digest": bundle.launch_digest,
            "environment_digest": bundle.environment_digests[0],
            "base_url": "http://127.0.0.1:8765",
            "policy_digest": manifest.policy_digest,
            "target_id": "neutral-target",
            "contract_digest": None,
            "settings_hash": None,
            "runtime_ref_digest": None,
            "seed_mode": "exact",
        },
        "execution_environment": bundle.environment_digests[0],
        "secret_names": ["SCORER_TOKEN"],
        "runtime_policy": {"stop_grace_seconds": 419.0, "cleanup_wait_seconds": 30.0},
        "evaluation": {
            "case_ids": ["c-3", "c-2"],
            "repeat_seeds": [17, 17],
            "evaluation_set_id": "fixed-set",
            "max_turns": 4,
            "timeout_seconds": 9,
            "metric_plan": {"primary": "score", "weights": [1, 0.5]},
            "max_failed_turns": 4,
            "call_retries": {"max_attempts": 3, "turn_max_attempts": 2},
            "concurrency": 1,
            "min_coverage": 0.95,
            "sample_retries": 3,
        },
    }
    assert PreparationStore(state).get_prepared(prepared.digest) == prepared
    assert not (tmp_path / "SCORER_EXECUTED").exists()
    assert not (tmp_path / "TARGET_EXECUTED").exists()


def test_evaluation_scorer_argv_allows_repeated_arguments(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    input_path = request["source"].input_path
    value = json.loads(input_path.read_text())
    value["scorer"]["argv"] = [sys.executable, "score.py", "same", "same"]
    input_path.write_text(json.dumps(value))

    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**request)
    prepared = store.prepare_evaluation(bundle.digest, selection={"count": 1})

    assert prepared.kind == "evaluation"


def test_evaluation_selection_is_explicit_ordered_and_changes_identity(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**_evaluation_request(tmp_path, monkeypatch))
    all_cases = store.prepare_evaluation(bundle.digest, selection={"all": True})
    first_two = store.prepare_evaluation(bundle.digest, selection={"count": 2})
    explicit = store.prepare_evaluation(
        bundle.digest,
        selection={"case_ids": ["c-1", "c-3"]},
        repeat_seeds=[29],
        evaluation_set_id="other-set",
        max_turns=2,
        timeout_seconds=3.5,
        metric_plan={"primary": "other"},
        runtime_policy={"stop_grace_seconds": 12, "cleanup_wait_seconds": 5},
    )

    assert all_cases.evaluation.case_ids == ("c-2", "c-1", "c-3")
    assert first_two.evaluation.case_ids == ("c-2", "c-1")
    assert explicit.evaluation.case_ids == ("c-1", "c-3")
    assert explicit.evaluation.repeat_seeds == (29,)
    assert explicit.runtime_policy.to_payload() == {"stop_grace_seconds": 12, "cleanup_wait_seconds": 5}
    assert len({all_cases.digest, first_two.digest, explicit.digest}) == 3

    for selection in ({}, {"all": False}, {"count": True}, {"count": 0},
                      {"case_ids": []}, {"case_ids": ["c-1", "c-1"]},
                      {"case_ids": ["missing"]}, {"all": True, "count": 1}):
        with pytest.raises(PreparationError) as caught:
            store.prepare_evaluation(bundle.digest, selection=selection)
        assert (caught.value.code, caught.value.stage) == ("invalid_request", "prepare")

    invalid_overrides = (
        {"repeat_seeds": [True]},
        {"repeat_seeds": [None]},
        {"evaluation_set_id": ""},
        {"max_turns": True},
        {"timeout_seconds": float("inf")},
        {"metric_plan": {"primary": float("nan")}},
        {"runtime_policy": {"stop_grace_seconds": True, "cleanup_wait_seconds": 5}},
    )
    for override in invalid_overrides:
        with pytest.raises(PreparationError) as caught:
            store.prepare_evaluation(bundle.digest, selection={"all": True}, **override)
        assert (caught.value.code, caught.value.stage) == ("invalid_request", "prepare")


def test_scripted_unpaired_evaluation_uses_null_seeds(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    input_path = request["source"].input_path
    value = json.loads(input_path.read_text())
    value["target"]["seed_mode"] = "unpaired"
    value["environment"] = {"kind": "scripted", "input_text_key": None}
    value["defaults"]["repeat_seeds"] = [None, None]
    input_path.write_text(json.dumps(value))
    launch_path = request["source"].launch_path
    launch = json.loads(launch_path.read_text())
    launch["identity"]["seed_mode"] = "unpaired"
    launch_path.write_text(json.dumps(launch))
    store = PreparationStore(tmp_path / "state")

    prepared = store.prepare_evaluation(
        store.create_bundle(**request).digest, selection={"count": 1}
    )

    assert prepared.evaluation.repeat_seeds == (None, None)


def test_full_evaluation_corpus_reserved_input_is_refused_even_when_unselected(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**_evaluation_request(tmp_path, monkeypatch, reserved=True))

    with pytest.raises(PreparationError) as caught:
        store.prepare_evaluation(bundle.digest, selection={"count": 1})

    assert (caught.value.code, caught.value.stage) == ("reserved_public_input", "prepare")
    assert list((tmp_path / "state" / "prepared").iterdir()) == []


def test_evaluation_bundle_refuses_a_state_directory_inside_declared_sources(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    state = request["source"].input_path.parent / "state"
    store = PreparationStore(state)

    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**request)

    assert (caught.value.code, caught.value.stage) == ("unsupported_input", "bundle")


def test_evaluation_bundle_refuses_launch_inside_excluded_paths(tmp_path, monkeypatch):
    from evolution_lab.preparation import EvaluationSource, PreparationError, PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    source = request["source"]
    root = source.input_path.parent
    # Move both evaluation documents into .git together, mirroring every relative
    # reference they make (seed/, evaluation.jsonl, scorer/, launcher.py) alongside them
    # unchanged (local_path() rejects ".." components outright, so no relative escape is
    # possible), so the pre-existing policy-vs-input_path check in _create_evaluation_bundle
    # and the reference/existence checks in Relocator.launch()/evaluation() cannot themselves
    # raise unsupported_input here; only the excluded_path guard on the documents' own
    # location can refuse this bundle.
    git_dir = root / ".git"
    git_dir.mkdir()
    shutil.copytree(root / "seed", git_dir / "seed")
    shutil.copy(root / "evaluation.jsonl", git_dir / "evaluation.jsonl")
    shutil.copytree(root / "scorer", git_dir / "scorer")
    shutil.copy(root / "launcher.py", git_dir / "launcher.py")
    shutil.copy(source.input_path, git_dir / "evaluation-input.json")
    shutil.copy(source.launch_path, git_dir / "launch.json")
    request["source"] = EvaluationSource(git_dir / "evaluation-input.json", git_dir / "launch.json")

    store = PreparationStore(tmp_path / "state")
    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**request)

    assert (caught.value.code, caught.value.stage) == ("unsupported_input", "bundle")


def test_campaign_derived_evaluation_requires_explicit_noncampaign_parameters(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    campaign, state = _campaign_bundle(tmp_path, monkeypatch)
    store = PreparationStore(state)

    with pytest.raises(PreparationError) as caught:
        store.prepare_evaluation(campaign.digest, selection={"all": True})
    assert (caught.value.code, caught.value.stage) == ("invalid_request", "prepare")

    prepared = store.prepare_evaluation(
        campaign.digest,
        selection={"case_ids": ["q2", "q1"]},
        repeat_seeds=[31],
        evaluation_set_id="campaign-eval",
        max_turns=3,
        timeout_seconds=4,
        metric_plan={"primary": "score"},
        max_failed_turns=1,
        call_retries="off",
    )
    assert prepared.kind == "evaluation"
    assert prepared.evaluation.case_ids == ("q2", "q1")
    assert prepared.secret_names == ()
    assert store.prepare_campaign(campaign.digest).kind == "campaign"


@pytest.mark.parametrize(
    ("pointer", "value"),
    [
        (("extra",), 1),
        (("defaults", "max_turns"), True),
        (("defaults", "timeout_seconds"), float("inf")),
        (("defaults", "repeat_seeds"), [None]),
        (("metric_plan",), {}),
        (("evaluation_set_id",), ""),
        (("scorer", "allowed_env"), ["UNDECLARED"]),
        (("environment", "input_text_key"), None),
        (("corpus", "extra"), 1),
        (("scorer", "extra"), 1),
        (("environment", "extra"), 1),
        (("defaults", "extra"), 1),
        (("policy", "extra"), 1),
    ],
)
def test_invalid_evaluation_input_is_refused_before_publication(tmp_path, monkeypatch, pointer, value):
    from evolution_lab.preparation import PreparationError, PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    path = request["source"].input_path
    payload = json.loads(path.read_text())
    target = payload
    for part in pointer[:-1]:
        target = target[part]
    target[pointer[-1]] = value
    path.write_text(json.dumps(payload))
    store = PreparationStore(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**request)

    assert caught.value.stage == "bundle"
    assert not list((tmp_path / "state" / "bundles").iterdir())


def test_input_v2_defaults_carry_the_budget_and_arguments_override_them(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    bundle, state = _evaluation_bundle(
        tmp_path, monkeypatch, schema="lab-evaluation-input/2",
        extra_defaults={"max_failed_turns": 3, "call_retries": {"max_attempts": 4, "turn_max_attempts": 1}},
    )
    store = PreparationStore(state)
    plan = store.prepare_evaluation(bundle.digest, selection={"all": True}).to_payload()["evaluation"]
    assert plan["max_failed_turns"] == 3
    assert plan["call_retries"] == {"max_attempts": 4, "turn_max_attempts": 1}
    off = store.prepare_evaluation(bundle.digest, selection={"all": True},
                                   max_failed_turns=2, call_retries="off").to_payload()["evaluation"]
    assert off["max_failed_turns"] == 2 and off["call_retries"] is None


def test_input_v1_and_v2_without_budget_resolve_retry(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    for schema in ("lab-evaluation-input/1", "lab-evaluation-input/2"):
        root = tmp_path / schema.replace("/", "-")
        root.mkdir()
        bundle, state = _evaluation_bundle(root, monkeypatch, schema=schema)
        plan = PreparationStore(state).prepare_evaluation(
            bundle.digest, selection={"all": True}).to_payload()["evaluation"]
        assert (plan["max_failed_turns"], plan["call_retries"]) == (4, {"max_attempts": 3, "turn_max_attempts": 2})


def test_budget_changes_the_prepared_digest(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    bundle, state = _evaluation_bundle(tmp_path, monkeypatch)
    store = PreparationStore(state)
    one = store.prepare_evaluation(bundle.digest, selection={"all": True})
    three = store.prepare_evaluation(bundle.digest, selection={"all": True}, max_failed_turns=3)
    assert one.digest != three.digest


@pytest.mark.parametrize("defaults", [
    {"max_failed_turns": 0}, {"max_failed_turns": True},
    {"call_retries": {"max_attempts": 11, "turn_max_attempts": 1}},
    {"call_retries": {"max_attempts": 2}}, {"call_retries": "on"},
])
def test_input_v2_rejects_an_invalid_budget(tmp_path, monkeypatch, defaults):
    from evolution_lab.preparation import PreparationError

    with pytest.raises(PreparationError) as error:
        _evaluation_bundle(tmp_path, monkeypatch, schema="lab-evaluation-input/2", extra_defaults=defaults)
    assert (error.value.code, error.value.stage) == ("invalid_request", "bundle")


# JSON null for a budget key means "not given": the default applies.
@pytest.mark.parametrize("defaults, applied", [
    ({"max_failed_turns": None}, (4, {"max_attempts": 3, "turn_max_attempts": 2})),
    ({"call_retries": None}, (4, {"max_attempts": 3, "turn_max_attempts": 2})),
    ({"max_failed_turns": None, "call_retries": None}, (4, {"max_attempts": 3, "turn_max_attempts": 2})),
    ({"max_failed_turns": None, "call_retries": {"max_attempts": 4, "turn_max_attempts": 1}},
     (4, {"max_attempts": 4, "turn_max_attempts": 1})),
    ({"max_failed_turns": 3, "call_retries": None}, (3, {"max_attempts": 3, "turn_max_attempts": 2})),
])
def test_input_v2_null_budget_means_the_default(tmp_path, monkeypatch, defaults, applied):
    from evolution_lab.preparation import PreparationStore

    bundle, state = _evaluation_bundle(
        tmp_path, monkeypatch, schema="lab-evaluation-input/2", extra_defaults=defaults)
    plan = PreparationStore(state).prepare_evaluation(
        bundle.digest, selection={"all": True}).to_payload()["evaluation"]
    assert (plan["max_failed_turns"], plan["call_retries"]) == applied


def test_input_v1_refuses_budget_keys(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError

    with pytest.raises(PreparationError) as error:
        _evaluation_bundle(tmp_path, monkeypatch, extra_defaults={"max_failed_turns": 2})
    assert error.value.stage == "bundle"


@pytest.mark.parametrize("value", ["off", {"max_attempts": 1, "turn_max_attempts": 1}])
def test_campaign_derived_evaluation_optional_budget_fields_preserve_explicit_values(tmp_path, monkeypatch, value):
    from evolution_lab.preparation import PreparationError, PreparationStore

    campaign, state = _campaign_bundle(tmp_path, monkeypatch)
    store = PreparationStore(state)
    explicit = dict(selection={"all": True}, repeat_seeds=[31], evaluation_set_id="campaign-eval",
                    max_turns=3, timeout_seconds=4, metric_plan={"primary": "score"})
    threshold = store.prepare_evaluation(campaign.digest, **explicit, max_failed_turns=2).evaluation
    assert threshold.max_failed_turns == 2
    assert threshold.call_retries == {"max_attempts": 3, "turn_max_attempts": 2}
    retries = store.prepare_evaluation(campaign.digest, **explicit, call_retries=value).evaluation
    assert retries.max_failed_turns == 3
    assert retries.call_retries == (None if value == "off" else value)
    plan = store.prepare_evaluation(campaign.digest, **explicit, max_failed_turns=2,
                                    call_retries=value).to_payload()["evaluation"]
    assert plan["max_failed_turns"] == 2
    assert plan["call_retries"] == (None if value == "off" else value)


@pytest.mark.parametrize("value", ["on", 5])
def test_prepare_evaluation_refuses_an_invalid_call_retries_argument(tmp_path, monkeypatch, value):
    from evolution_lab.preparation import PreparationError, PreparationStore

    bundle, state = _evaluation_bundle(tmp_path, monkeypatch, schema="lab-evaluation-input/2")
    with pytest.raises(PreparationError) as error:
        PreparationStore(state).prepare_evaluation(bundle.digest, selection={"all": True}, call_retries=value)
    assert (error.value.code, error.value.stage) == ("invalid_request", "prepare")


def test_old_prepared_descriptor_digest_unchanged():
    from evolution_lab.preparation.models import PreparedExecutionDescriptor
    from evolution_lab.preparation.storage import canonical_json_bytes

    fixture = json.loads((Path(__file__).parent / "fixtures/preparation/dev59-evaluation-descriptor.json").read_text())
    descriptor = PreparedExecutionDescriptor.from_payload(fixture["descriptor"])
    assert descriptor.digest == fixture["prepared_digest"]
    assert canonical_json_bytes(descriptor.to_payload()).decode() == fixture["canonical_descriptor"]
    assert descriptor.evaluation.min_coverage == 1.0
    assert descriptor.evaluation.sample_retries is None
    assert descriptor.evaluation.scorer_concurrency == 1


def test_new_plan_always_writes_min_coverage(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    bundle, state = _evaluation_bundle(tmp_path, monkeypatch)
    prepared = PreparationStore(state).prepare_evaluation(bundle.digest, selection={"all": True})
    payload = prepared.evaluation.to_payload()
    assert payload["min_coverage"] == 0.95
    assert payload["sample_retries"] == 3
    assert "scorer_concurrency" not in payload
    assert PreparationStore(state).get_prepared(prepared.digest) == prepared


@pytest.mark.parametrize("value", [0, 8, 9, -1, True, 1.5])
def test_sample_retries_range(tmp_path, monkeypatch, value):
    from evolution_lab.preparation import PreparationError, PreparationStore

    bundle, state = _evaluation_bundle(tmp_path, monkeypatch)
    store = PreparationStore(state)
    if type(value) is int and 0 <= value <= 8:
        prepared = store.prepare_evaluation(bundle.digest, selection={"all": True}, sample_retries=value)
        assert prepared.evaluation.sample_retries == value
        assert store.get_prepared(prepared.digest) == prepared
    else:
        with pytest.raises(PreparationError) as caught:
            store.prepare_evaluation(bundle.digest, selection={"all": True}, sample_retries=value)
        assert caught.value.code == "invalid_request"
        assert list((state / "prepared").iterdir()) == []


def _set_session_limit(request, limit):
    path = request["source"].launch_path
    payload = json.loads(path.read_text())
    payload["limits"]["max_concurrent_sessions"] = limit
    path.write_text(json.dumps(payload))


def test_concurrency_above_sessions_refused(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    _set_session_limit(request, 2)
    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**request)
    with pytest.raises(PreparationError) as caught:
        store.prepare_evaluation(bundle.digest, selection={"all": True}, concurrency=3)
    assert caught.value.code == "invalid_request"
    assert "concurrency" in str(caught.value)
    assert list((tmp_path / "state/prepared").iterdir()) == []


def test_request_args_override_input_defaults(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch, schema="lab-evaluation-input/2",
                                  extra_defaults={"sample_retries": 2, "min_coverage": 0.8,
                                                  "concurrency": 2, "scorer_concurrency": 3})
    _set_session_limit(request, 3)
    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**request)
    defaults = store.prepare_evaluation(bundle.digest, selection={"all": True}).evaluation
    assert (defaults.sample_retries, defaults.min_coverage, defaults.concurrency,
            defaults.scorer_concurrency) == (2, 0.8, 2, 3)
    prepared = store.prepare_evaluation(bundle.digest, selection={"all": True}, sample_retries=0,
                                       min_coverage=1.0, concurrency=3, scorer_concurrency=2)
    plan = prepared.evaluation
    assert (plan.sample_retries, plan.min_coverage, plan.concurrency, plan.scorer_concurrency) == (0, 1.0, 3, 2)
    assert store.get_prepared(prepared.digest) == prepared
    assert prepared.digest != store.prepare_evaluation(bundle.digest, selection={"all": True}).digest


@pytest.mark.parametrize("field, value, input_default", [
    ("min_coverage", 0, False), ("min_coverage", 1.01, False), ("min_coverage", True, False),
    ("min_coverage", float("nan"), False), ("min_coverage", float("inf"), False),
    ("concurrency", 0, False), ("concurrency", True, False), ("concurrency", 1.5, False),
    ("scorer_concurrency", 0, False), ("scorer_concurrency", True, False), ("scorer_concurrency", 1.5, False),
    pytest.param("min_coverage", 10**400, False, id="min_coverage-huge-request"),
    pytest.param("min_coverage", 10**400, True, id="min_coverage-huge-default"),
])
def test_invalid_plan_field_refused(tmp_path, monkeypatch, field, value, input_default):
    from evolution_lab.preparation import PreparationError, PreparationStore

    state = tmp_path / "state"
    if input_default:
        with pytest.raises(PreparationError) as caught:
            _evaluation_bundle(tmp_path, monkeypatch, extra_defaults={field: value})
    else:
        bundle, state = _evaluation_bundle(tmp_path, monkeypatch)
        with pytest.raises(PreparationError) as caught:
            PreparationStore(state).prepare_evaluation(bundle.digest, selection={"all": True}, **{field: value})
    assert caught.value.code == "invalid_request"
    assert list((state / "prepared").iterdir()) == []


def test_identity_carries_concurrency_and_retries(tmp_path):
    from evolution.tree.execution.interaction import RolloutBudget, SemanticIdentity
    from evolution_lab._fixed_execution import build_fixed_identity
    from test_evaluation import bindings

    values = bindings(tmp_path)
    identity, _ = build_fixed_identity(
        candidate_digest=values["candidate"].digest,
        target=SemanticIdentity(**values["identity"].to_payload()["target"]),
        environment=values["environment"], scorer=values["scorer"], evaluation_set_id="neutral-set",
        cases=values["cases"], repeat_seeds=[17], budget=RolloutBudget(max_turns=1, timeout_seconds=10),
        metric_plan={"primary": "quality"}, concurrency=3, scorer_concurrency=2, sample_retries=1,
    )
    payload = identity.to_payload()
    assert payload["row_provenance"] == 1
    assert (payload["max_concurrency"], payload["max_scorer_concurrency"], payload["sample_retries"]) == (3, 2, 1)


def test_evaluation_reserved_marker_in_scorer_is_refused(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore, PreparationError
    request = _evaluation_request(tmp_path, monkeypatch)
    path = request['source'].input_path
    value = json.loads(path.read_text())
    value['scorer']['allowed_env'].append('EVOLUTION_LAB_SCOPE')
    path.write_text(json.dumps(value))
    request['secret_names'] = (*request['secret_names'], 'EVOLUTION_LAB_SCOPE')
    store = PreparationStore(tmp_path / 'state')
    with pytest.raises(PreparationError) as failure:
        store.create_bundle(**request)
    assert failure.value.code == 'reserved_environment_name'
    assert not list((tmp_path / 'state/bundles').iterdir())
