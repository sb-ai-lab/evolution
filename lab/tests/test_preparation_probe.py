"""dev37 Task 3: Bundle source kind ``probe`` (spec 2026-10-02 §2.2)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from test_preparation_evaluation import _descriptor


def _probe_request(tmp_path: Path, monkeypatch):
    from evolution_lab.preparation import EnvironmentInput, PreparationStore, ProbeSource, SourceRoot
    from evolution_lab.preparation.environment import CapturedEnvironment
    from evolution_lab.projects.entry import ENTRY_FILE, probe_launch, read_target_entry
    import evolution_lab.preparation.bundle as bundle_module

    descriptor = _descriptor()
    monkeypatch.setattr(
        bundle_module,
        "capture_environment",
        lambda value, *, execution: CapturedEnvironment(descriptor, '{"archive_wheels":[]}'),
    )
    place = tmp_path / "impl"
    (place / "agent").mkdir(parents=True)
    (place / "agent" / "demo_agent.py").write_text("def build():\n    return None\n")
    (place / ENTRY_FILE).write_text(json.dumps({
        "schema": "lab-target-entry/1",
        "agent": {"module": "demo_agent", "attribute": "build", "import_root": "agent"},
        "program_paths": ["agent"], "environment": {"kind": "service"},
        "env": {"A": "1"}, "secret_names": ["KEY"],
    }), encoding="utf-8")
    entry = read_target_entry(place)
    launch = probe_launch(entry, target_id="lab-probe-p-i", base_url="http://127.0.0.1:5999")
    request = dict(
        source=ProbeSource(place / ENTRY_FILE, launch),
        environments=[EnvironmentInput(Path(sys.executable), (tmp_path / "retained.whl",))],
        env=entry.env,
        secret_names=list(entry.secret_names),
        roots=[SourceRoot("implementation", place)],
    )
    return PreparationStore(tmp_path / "state"), request


def _stored(store, digest):
    return store._storage.get_bundle(digest)


def test_probe_bundle_from_entry_with_service_environment(tmp_path, monkeypatch):
    from evolution_lab.preparation.internal_manifest import load_internal_manifest

    store, request = _probe_request(tmp_path, monkeypatch)
    summary = store.create_bundle(**request)
    assert summary.source_kind == "probe"
    stored = _stored(store, summary.digest)
    assert stored.descriptor.conformance_entrypoint == ".lab/probe-policy.json"
    assert stored.descriptor.entrypoint == "inputs/implementation/lab-target-entry.json"
    assert stored.descriptor.launch == ".lab/launch.json"
    manifest = json.loads((stored.path / ".lab/probe-policy.json").read_text())
    assert manifest["schema"] == "evolution-v2-manifest/1"
    assert manifest["artifacts"] == {"excludes": [], "dependency_paths": [], "writable_paths": []}
    assert manifest["target"]["seed_mode"] == "none"
    assert manifest["seed_root"] == "../inputs/implementation"
    assert load_internal_manifest(stored.path / ".lab/probe-policy.json").target.base_url == "http://127.0.0.1:5999"
    launch = json.loads((stored.path / stored.descriptor.launch).read_text())
    assert launch["environment_ref"] == stored.descriptor.environments[-1].digest
    assert launch["identity"]["runtime_ref_digest"] == stored.descriptor.environments[-1].digest
    assert launch["policy"] == ".lab/probe-policy.json"
    assert launch["program_paths"] == ["inputs/implementation/agent"]
    assert launch["agent"]["import_root"] == "inputs/implementation/agent"
    assert (stored.path / "inputs/implementation/agent/demo_agent.py").is_file()
    # The source launch is not mutated by preparation.
    assert "environment_ref" not in request["source"].launch
    assert request["source"].launch["identity"]["runtime_ref_digest"] is None


def test_entry_rewritten_after_the_launch_was_synthesized_is_inputs_changed(tmp_path, monkeypatch):
    """ADR-0021: the Bundle never pairs one entry with a launch synthesized from another (codex r1 E2)."""
    from evolution_lab.preparation import PreparationError
    from evolution_lab.projects.entry import ENTRY_FILE

    store, request = _probe_request(tmp_path, monkeypatch)
    entry = tmp_path / "impl" / ENTRY_FILE
    document = json.loads(entry.read_text(encoding="utf-8"))
    entry.write_text(json.dumps({**document, "env": {"A": "2"}}), encoding="utf-8")
    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**request)
    assert (caught.value.code, caught.value.stage) == ("inputs_changed", "bundle")
    assert not list((tmp_path / "state" / "bundles").iterdir())
    # The digest is of the declaration, not of its bytes: the same entry written another way is the same entry.
    entry.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    assert store.create_bundle(**request).source_kind == "probe"


def test_probe_bundle_cannot_be_prepared(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError

    store, request = _probe_request(tmp_path, monkeypatch)
    digest = store.create_bundle(**request).digest
    with pytest.raises(PreparationError) as campaign:
        store.prepare_campaign(digest)
    assert (campaign.value.code, campaign.value.stage) == ("artifacts_invalid", "prepare")
    with pytest.raises(PreparationError) as evaluation:
        store.prepare_evaluation(digest, selection={"count": 1})
    assert (evaluation.value.code, evaluation.value.stage) == ("artifacts_invalid", "prepare")
    assert list((tmp_path / "state" / "prepared").iterdir()) == []


def test_probe_bundle_refuses_a_launch_that_already_names_an_environment(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, ProbeSource

    store, request = _probe_request(tmp_path, monkeypatch)
    source = request["source"]
    request["source"] = ProbeSource(source.entry_path, {**source.launch, "environment_ref": "d" * 64})
    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**request)
    assert (caught.value.code, caught.value.stage) == ("invalid_request", "bundle")


def test_probe_launch_names_the_target_environment_when_one_is_given(tmp_path, monkeypatch):
    from evolution_lab.preparation import EnvironmentInput
    from evolution_lab.preparation.environment import CapturedEnvironment
    from evolution_lab.preparation.models import EnvironmentDescriptor
    import evolution_lab.preparation.bundle as bundle_module

    store, request = _probe_request(tmp_path, monkeypatch)
    service = _descriptor()
    target_python = tmp_path / "target-venv" / "bin" / "python"
    target = EnvironmentDescriptor.from_payload({**service.to_payload(), "executable": str(target_python)})
    assert target.digest != service.digest
    by_executable = {Path(sys.executable).absolute(): service, target_python: target}
    monkeypatch.setattr(
        bundle_module,
        "capture_environment",
        lambda value, *, execution: CapturedEnvironment(by_executable[value.executable.absolute()], '{"archive_wheels":[]}'),
    )
    request["environments"] = [request["environments"][0], EnvironmentInput(target_python, ())]
    stored = _stored(store, store.create_bundle(**request).digest)
    assert [environment.digest for environment in stored.descriptor.environments] == [service.digest, target.digest]
    launch = json.loads((stored.path / stored.descriptor.launch).read_text())
    assert launch["environment_ref"] == target.digest
    assert launch["identity"]["runtime_ref_digest"] == target.digest


@pytest.mark.parametrize("roots", ["extra_root", "parent_root"])
def test_probe_bundle_requires_the_place_as_its_only_root(tmp_path, monkeypatch, roots):
    from evolution_lab.preparation import PreparationError, ProbeSource, SourceRoot
    from evolution_lab.projects.entry import ENTRY_FILE
    import evolution_lab.preparation.bundle as bundle_module

    store, request = _probe_request(tmp_path, monkeypatch)
    place = request["roots"][0].source_path
    if roots == "extra_root":
        extra = tmp_path / "extra"
        extra.mkdir()
        request["roots"] = [request["roots"][0], SourceRoot("extra", extra)]
    else:  # the entry lies under the only root, but that root is not the place
        outer = tmp_path / "outer"
        shutil.copytree(place, outer / "impl")
        request["roots"] = [SourceRoot("implementation", outer)]
        request["source"] = ProbeSource(outer / "impl" / ENTRY_FILE, request["source"].launch)

    def no_capture(value, *, execution):
        raise AssertionError("the root check must come before any capture")

    monkeypatch.setattr(bundle_module, "capture_environment", no_capture)
    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**request)
    assert (caught.value.code, caught.value.stage) == ("invalid_request", "bundle")
    assert not (tmp_path / "state" / "bundles").exists() or list((tmp_path / "state" / "bundles").iterdir()) == []


def test_bundle_request_refuses_probe_kind():
    from evolution_lab.service.codecs import decode_bundle_request
    from evolution_lab.service.errors import ServiceError

    with pytest.raises(ServiceError) as caught:
        decode_bundle_request({"schema": "lab-service-bundle-request/2",
                               "source": {"kind": "probe", "entry_path": "/x"}, "environments": [{"kind": "service"}],
                               "env": {}, "secret_names": [], "roots": [], "path_bindings": []})
    assert caught.value.code == "invalid_request"


def test_descriptor_round_trips_probe_kind(tmp_path, monkeypatch):
    from evolution_lab.preparation.models import BundleDescriptor

    store, request = _probe_request(tmp_path, monkeypatch)
    stored = _stored(store, store.create_bundle(**request).digest)
    again = BundleDescriptor.from_payload(stored.descriptor.to_payload(), "bundle")
    assert again.source_kind == "probe" and again.conformance_entrypoint != again.entrypoint
    assert again == stored.descriptor


@pytest.mark.parametrize('declaration', ['secret_names', 'env'])
def test_probe_reserved_marker_is_refused(tmp_path, monkeypatch, declaration):
    from dataclasses import replace
    from evolution_lab.preparation import PreparationError
    from evolution_lab.projects.entry import probe_launch, read_target_entry
    store, request = _probe_request(tmp_path, monkeypatch)
    source = request['source']
    value = json.loads(source.entry_path.read_text())
    if declaration == 'secret_names':
        value['secret_names'].append('EVOLUTION_LAB_SCOPE')
    else:
        value['env']['EVOLUTION_LAB_SCOPE'] = 'operator-value'
    source.entry_path.write_text(json.dumps(value))
    entry = read_target_entry(source.entry_path.parent)
    launch = probe_launch(entry, target_id=source.launch['identity']['target_id'],
        base_url=source.launch['endpoint']['base_url'])
    request.update(source=replace(source, launch=launch), env=entry.env, secret_names=entry.secret_names)
    with pytest.raises(PreparationError) as failure:
        store.create_bundle(**request)
    assert failure.value.code == 'reserved_environment_name'
    assert not list((tmp_path / 'state/bundles').iterdir())
