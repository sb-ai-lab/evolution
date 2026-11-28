"""Launch /3 admission, immutable normalization and Bundle consistency (ADR-0026)."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from evolution_lab.preparation.models import PreparationError
from evolution_lab.preparation.relocation import Relocator
from evolution_lab.preparation.storage import PreparationStorage, canonical_json_bytes
from test_preparation_bundle import fixture, launch_v2, _with_distinct_target_launch_v2


def _launch(root, request):
    path = request["source"].launch_path
    raw = launch_v2(json.loads(path.read_text()))
    raw.update(schema="lab-target-launch/3", reflector_probe=None,
               turn={"empty_answer": "target_failed"})
    return raw


def _probe(raw):
    return dict(environment_ref=raw["environment_ref"], executable=sys.executable,
                args=[{"bundle_path": "launcher.py"}, {"literal": "--port"}, {"probe_port": None}],
                startup_timeout_seconds=60, stop_grace_seconds=5, max_restarts=3)


def _normalize(bundle, root, request, raw, *, descriptors=None):
    descriptors = descriptors or (bundle.capture_environment(request["environments"][0], execution=True).descriptor,)
    return Relocator(request["roots"], ()).launch(
        request["source"].launch_path, raw, descriptors, request["env"], request["secret_names"])


@pytest.mark.parametrize("ports", [0, 1, 2])
def test_launch_v3_normalizes_probe_paths_without_mutating_source(tmp_path, monkeypatch, ports):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    raw["reflector_probe"] = _probe(raw)
    raw["reflector_probe"]["args"] = [{"bundle_path": "launcher.py"}, {"literal": "--port"}] + [{"probe_port": None}] * ports
    before = copy.deepcopy(raw)
    normalized = _normalize(bundle, root, request, raw)
    assert normalized["reflector_probe"]["args"] == [
        {"bundle_path": "inputs/project/launcher.py"}, {"literal": "--port"}] + [{"probe_port": None}] * ports
    assert normalized["turn"] == {"empty_answer": "target_failed"}
    assert raw == before


@pytest.mark.parametrize("key,value,code", [
    ("environment_ref", "d" * 64, "environment_invalid"),
    ("executable", "/different/python", "environment_invalid"),
    ("args", None, "unsupported_input"),
    ("args", ["port"], "unsupported_input"),
    ("args", [{"literal": 1}], "unsupported_input"),
    ("args", [{"bundle_path": "missing.py"}], "unsupported_input"),
    ("args", [{"bundle_path": "../launcher.py"}], "unsupported_input"),
    ("args", [{"probe_port": 9}], "unsupported_input"),
    ("args", [{"probe_port": False}], "unsupported_input"),
    ("args", [{"unknown": None}], "unsupported_input"),
    ("args", [{"literal": "x", "probe_port": None}], "unsupported_input"),
    *[(key, value, "unsupported_input") for key in ("startup_timeout_seconds", "stop_grace_seconds")
      for value in (0, -1, True, "1", None, float("nan"), float("inf"), -float("inf"))],
    *[("max_restarts", value, "unsupported_input") for value in (-1, True, 1.5, "1", None)],
])
def test_launch_v3_refuses_invalid_probe_fields(tmp_path, monkeypatch, key, value, code):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    raw["reflector_probe"] = {**_probe(raw), key: value}
    with pytest.raises(PreparationError) as caught:
        _normalize(bundle, root, request, raw)
    assert (caught.value.code, caught.value.stage) == (code, "bundle")


@pytest.mark.parametrize("change", ["not_object", "extra", *list(_probe({"environment_ref": "x"}))])
def test_launch_v3_requires_exact_probe_keys(tmp_path, monkeypatch, change):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    probe = _probe(raw)
    if change == "extra":
        probe["extra"] = 1
    elif change != "not_object":
        del probe[change]
    raw["reflector_probe"] = [] if change == "not_object" else probe
    with pytest.raises(PreparationError) as caught:
        _normalize(bundle, root, request, raw)
    assert caught.value.code == "unsupported_input"


def test_launch_v3_probe_requires_one_matching_environment(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    raw["reflector_probe"] = _probe(raw)
    descriptor = bundle.capture_environment(request["environments"][0], execution=True).descriptor
    # The target environment stays unique; only the probe's separate environment is ambiguous.
    other = type(descriptor).from_payload({**descriptor.to_payload(), "executable": "/other/python"})
    raw["reflector_probe"].update(environment_ref=other.digest, executable=other.executable)
    with pytest.raises(PreparationError) as caught:
        _normalize(bundle, root, request, raw, descriptors=(descriptor, other, other))
    assert caught.value.code == "environment_invalid"


@pytest.mark.parametrize("turn", [None, [], {}, {"empty_answer": "bad"}, {"empty_answer": []},
                                  {"empty_answer": True}, {"empty_answer": "ok", "extra": 1}])
def test_launch_v3_refuses_invalid_turn_policy(tmp_path, monkeypatch, turn):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = {**_launch(root, request), "turn": turn}
    with pytest.raises(PreparationError) as caught:
        _normalize(bundle, root, request, raw)
    assert caught.value.code == "unsupported_input"


@pytest.mark.parametrize("key", ["reflector_probe", "turn"])
def test_launch_v3_requires_new_keys(tmp_path, monkeypatch, key):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    del raw[key]
    with pytest.raises(PreparationError) as caught:
        _normalize(bundle, root, request, raw)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("manifest_probe,launch_probe", [(False, False), (True, False), (True, True), (False, True)])
def test_campaign_bundle_probe_consistency(tmp_path, monkeypatch, manifest_probe, launch_probe):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    if launch_probe:
        raw["reflector_probe"] = _probe(raw)
        raw["reflector_probe"]["max_restarts"] = 0
    request["source"].launch_path.write_text(json.dumps(raw))
    manifest_path = request["source"].manifest_path
    manifest = json.loads(manifest_path.read_text())
    manifest["reflector"]["probe"] = {"base_url": "http://127.0.0.1:5998"} if manifest_probe else None
    manifest_path.write_text(json.dumps(manifest))
    if launch_probe and not manifest_probe:
        with pytest.raises(PreparationError) as caught:
            bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
        assert (caught.value.code, caught.value.stage) == ("manifest_invalid", "bundle")
        assert "reflector_probe" in caught.value.failure.message and "reflector.probe" in caught.value.failure.message
    else:
        stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
        saved = json.loads((stored.path / stored.descriptor.launch).read_bytes())
        assert saved["reflector_probe"] is None if not launch_probe else saved["reflector_probe"]["max_restarts"] == 0


@pytest.mark.parametrize("kind", ["evaluation", "probe"])
@pytest.mark.parametrize("declares_probe", [False, True])
def test_non_campaign_bundles_allow_only_null_reflector_probe(tmp_path, monkeypatch, kind, declares_probe):
    from evolution_lab.preparation import PreparationStore, ProbeSource
    from test_preparation_evaluation import _evaluation_request
    from test_preparation_probe import _probe_request

    if kind == "evaluation":
        request = _evaluation_request(tmp_path, monkeypatch)
        store = PreparationStore(tmp_path / "state")
        path = request["source"].launch_path
        raw = launch_v2(json.loads(path.read_text()))
    else:
        store, request = _probe_request(tmp_path, monkeypatch)
        raw = dict(request["source"].launch)
    raw.update(schema="lab-target-launch/3", reflector_probe={"declared": True} if declares_probe else None,
               turn={"empty_answer": "ok"})
    if kind == "evaluation":
        path.write_text(json.dumps(raw))
    else:
        request["source"] = ProbeSource(request["source"].entry_path, raw)
    if declares_probe:
        with pytest.raises(PreparationError) as caught:
            store.create_bundle(**request)
        assert (caught.value.code, caught.value.stage) == ("invalid_request", "bundle")
    else:
        assert store.create_bundle(**request).source_kind == kind


@pytest.mark.parametrize("empty_answer", ["ok", "target_failed"])
def test_launch_v3_runtime_is_v2_plus_explicit_turn(tmp_path, monkeypatch, empty_answer):
    from evolution.tree import load_manifest_v2
    from evolution_lab.owner.target import runtime_config, target_argv

    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    raw["turn"] = {"empty_answer": empty_answer}
    normalized = _normalize(bundle, root, request, raw)
    binding = SimpleNamespace(launch=normalized, launch_digest="a" * 64, bundle_digest="b" * 64,
        manifest=load_manifest_v2(request["source"].manifest_path), bundle_path=root,
        target_environment=SimpleNamespace(executable="/target/python"))
    config = runtime_config(tmp_path, binding, SimpleNamespace(id="target"))
    assert config.pop("turn") == {"empty_answer": empty_answer}
    assert config["schema"] == "lab-target-runtime/2"
    assert target_argv(binding) == ["/target/python", "-I", "-m", "evolution_lab.target.launch"]
    binding.launch = {key: value for key, value in normalized.items() if key not in ("turn", "reflector_probe")}
    binding.launch["schema"] = "lab-target-launch/2"
    assert config == runtime_config(tmp_path, binding, SimpleNamespace(id="target"))


def test_launch_v3_keeps_version_mismatch_warning(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationStore

    bundle, root, request = fixture(tmp_path, monkeypatch)
    capture = _with_distinct_target_launch_v2(bundle, root, request, tmp_path, target_lab="0.0.3.dev3")
    monkeypatch.setattr(bundle, "capture_environment", capture)
    raw = _launch(root, request)
    request["source"].launch_path.write_text(json.dumps(raw))
    summary = PreparationStore(tmp_path / "state").create_bundle(**request)
    assert [warning.code for warning in summary.warnings] == ["lab_version_mismatch"]


@pytest.mark.parametrize("schema,expected_sha256", [
    (1, "60a57cfda474e7fe504a341a031ef6bd309412f9feac98fac8aca68af9b7c8bc"),
    (2, "197644fc7297cb4086d89fb62aa7a06fdb656008019006446f2de1a6998f4b26"),
])
def test_legacy_launch_bytes_and_digest_unchanged(tmp_path, monkeypatch, schema, expected_sha256):
    """Hashes captured on Task1 base a068db1c, with a stable neutral executable."""
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = json.loads(request["source"].launch_path.read_text())
    descriptor = bundle.capture_environment(request["environments"][0], execution=True).descriptor
    descriptor = type(descriptor).from_payload({**descriptor.to_payload(), "executable": "/neutral/python"})
    raw["environment_ref"] = descriptor.digest
    raw["command"]["executable"] = descriptor.executable
    if schema == 2:
        raw = launch_v2(raw)
    data = canonical_json_bytes(_normalize(bundle, root, request, raw, descriptors=(descriptor,)))
    assert hashlib.sha256(data).hexdigest() == expected_sha256


def test_launch_v3_probe_can_use_a_distinct_environment_and_empty_args(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = _launch(root, request)
    descriptor = bundle.capture_environment(request["environments"][0], execution=True).descriptor
    other = type(descriptor).from_payload({**descriptor.to_payload(), "executable": "/other/python"})
    raw["reflector_probe"] = {**_probe(raw), "environment_ref": other.digest,
        "executable": other.executable, "args": [], "startup_timeout_seconds": .5,
        "stop_grace_seconds": .25, "max_restarts": 0}
    normalized = _normalize(bundle, root, request, raw, descriptors=(descriptor, other))
    assert normalized["reflector_probe"] == raw["reflector_probe"]
