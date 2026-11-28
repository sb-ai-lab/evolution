"""Pinned core scaffolds /15; Lab preserves it with the observe smoke profile."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from evolution_lab._scaffold_manifest import LAB_SCHEMA_15, SCAFFOLD_SCHEMA, as_manifest_15

B1 = Path(__file__).parent / "fixtures" / "scaffold-b1-manifest-15.json"


def _b1() -> dict:
    return json.loads(B1.read_text(encoding="utf-8"))


def test_scaffold_schema_is_15():
    assert SCAFFOLD_SCHEMA == "evolution-v2-manifest/15" == _b1()["schema"]


def test_live_core_scaffold_equals_the_b1_fixture(tmp_path):
    """Ties the fixture to the installed core: a core that changes its scaffold breaks here, not silently."""
    from evolution.tree.reference_target_cli import scaffold
    live = json.loads(scaffold(tmp_path / "s").manifest.read_text(encoding="utf-8"))
    assert live == _b1()
    assert json.dumps(live, indent=2) == json.dumps(_b1(), indent=2)


@pytest.mark.parametrize("mutate, where", [
    (lambda d: d.update(extra=1), "extra"),
    (lambda d: d.pop("grouper"), "top-level keys"),
    (lambda d: d.pop("role_visibility"), "top-level keys"),
    (lambda d: d["campaign"].update(extra=1), "extra"),
    (lambda d: d["campaign"].pop("round_stop"), "round_stop"),
    (lambda d: d["campaign"]["gate"].update(extra=1), "extra"),
    (lambda d: d["campaign"]["gate"].pop("stability"), "stability"),
    (lambda d: d["campaign"]["evaluation"].update(extra=1), "campaign.evaluation"),
    (lambda d: d["scorer"].pop("metric_contract"), "scorer keys"),
    (lambda d: d["scorer"].update(extra=1), "scorer keys"),
    (lambda d: d["editor"].update(extra=1), "editor keys"),
    (lambda d: d["reflector"].update(extra=1), "reflector keys"),
    (lambda d: d["grouper"].update(extra=1), "grouper keys"),
    (lambda d: d["grouper"].update(kind="command"), "grouper.kind"),
    (lambda d: d["role_visibility"].update(readable_paths=["x/**"]), "role_visibility"),
    (lambda d: d.update(scorer="x"), "scorer keys"),
    (lambda d: d.update(grouper=[]), "grouper keys"),
    (lambda d: d.update(role_visibility=None), "role_visibility"),
    (lambda d: d["reflector"].update(grouping={"kind": "fields", "features": []}), "grouping"),
    (lambda d: d.update(schema="evolution-v2-manifest/14"), "evolution-v2-manifest/15"),
])
def test_as_manifest_15_refuses_changed_scaffold_structure(mutate, where):
    document = _b1()
    mutate(document)
    with pytest.raises(ValueError, match=where):
        as_manifest_15(document)


def test_as_manifest_15_is_the_observe_smoke_profile():
    """Observe and retry profiles preserve every unaffected scaffold field."""
    source = _b1()
    out = as_manifest_15(source)
    assert out["schema"] == LAB_SCHEMA_15 == "evolution-v2-manifest/15"
    assert out["campaign"]["evaluation"] == {"repeats_per_question": 1}
    assert out["campaign"]["gate"] == {
        "mode": "observe", "stability": "stable", "focus": {"margin": 0},
        "causal": {"tolerance": "measured_noise"}, "provenance": {"kind": "reported"},
        "fresh_parent": True, "global": "off"}
    assert list(out["campaign"]["gate"]) == ["mode", "stability", "focus", "causal", "provenance",
                                             "fresh_parent", "global"]
    for key in ("round_stop", "baseline_refresh_after_rounds"):
        assert out["campaign"][key] == source["campaign"][key]
    for key in ("grouper", "role_visibility"):
        assert out[key] == source[key]
    assert out["scorer"]["metric_contract"] == source["scorer"]["metric_contract"]
    rest = {k: v for k, v in out.items() if k not in ("campaign", "reflector")}
    assert rest == {k: v for k, v in source.items() if k not in ("campaign", "reflector")}
    assert {k: v for k, v in out["reflector"].items() if k != "max_consecutive_failures"} == \
           {k: v for k, v in source["reflector"].items() if k != "max_consecutive_failures"}
    assert list(out) == list(source)
    changed = {"gate", "evaluation", "sample_retries", "call_retries", "budget", "indeterminate"}
    assert {k: v for k, v in out["campaign"].items() if k not in changed} == \
           {k: v for k, v in source["campaign"].items() if k not in changed}
    assert {k: v for k, v in out["campaign"]["budget"].items() if k != "max_failed_turns"} == \
           {k: v for k, v in source["campaign"]["budget"].items() if k != "max_failed_turns"}


def test_as_manifest_15_does_not_mutate_its_input():
    source = _b1()
    before = copy.deepcopy(source)
    as_manifest_15(source)
    assert source == before


@pytest.mark.parametrize("mutate, where", [
    (lambda d: d.update(extra=1), "top-level"),
    (lambda d: d["campaign"].update(extra=1), "campaign"),
    (lambda d: d["campaign"]["gate"].update(extra=1), "campaign.gate"),
    (lambda d: d["campaign"]["gate"]["focus"].update(extra=1), "campaign.gate.focus"),
    (lambda d: d["campaign"]["gate"]["causal"].pop("confidence"), "campaign.gate.causal"),
    (lambda d: d["scorer"].pop("metric_contract"), "scorer"),
    (lambda d: d["grouper"].update(kind="chat"), "grouper.kind"),
])
def test_as_manifest_15_refuses_a_changed_scaffold(mutate, where):
    source = _b1()
    mutate(source)
    with pytest.raises(ValueError, match=re.escape(where)):
        as_manifest_15(source)


def test_scaffold_15_is_admitted_by_the_pinned_core(tmp_path):
    from evolution.tree import load_manifest_v2
    from scaffold_15 import scaffold_15
    sample = scaffold_15(tmp_path / "s")
    manifest = load_manifest_v2(sample.manifest)
    assert manifest.schema == "evolution-v2-manifest/15" and manifest.workflow is not None


@pytest.mark.parametrize("schema", ["evolution-v2-manifest/11", "evolution-v2-manifest/14"])
@pytest.mark.parametrize("path", ["bundle", "form", "legacy", "connect"])
def test_retired_campaign_schema_is_refused_before_any_effect(tmp_path, monkeypatch, schema, path):
    """Removing the admission gate must fail with the existing public refusal, before creating files."""
    from scaffold_15 import scaffold_15
    from test_preparation_bundle import fixture
    from evolution_lab.client import ClientError, connect
    from evolution_lab.preparation.models import PreparationError
    from evolution_lab.preparation.storage import PreparationStorage
    from evolution_lab.projects.package import read_package_entry, validate_form
    from evolution_lab.recipe import prepare_inputs

    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    manifest = request["source"].manifest_path
    raw = json.loads(manifest.read_text())
    raw["schema"] = schema
    manifest.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")
    entry = root / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    before = sorted(tmp_path.rglob("*"))
    if path == "bundle":
        with pytest.raises(PreparationError) as caught:
            bundle.create_bundle(storage, **request)
        assert (caught.value.code, caught.value.stage) == ("unsupported_schema", "bundle")
    elif path == "form":
        assert {"code": "unsupported_schema", "detail": "manifest schema is not the pinned core's"} in \
            validate_form(read_package_entry(entry), place=root)
    elif path == "legacy":
        with pytest.raises(ValueError) as caught:
            prepare_inputs(manifest)
        assert str(caught.value) == "Lab requires a native campaign manifest: evolution-v2-manifest/15"
    else:
        with pytest.raises(ClientError) as caught:
            connect(manifest)
        assert caught.value.failure.public() == {"code": "manifest_invalid", "stage": "connect"}
    assert sorted(tmp_path.rglob("*")) == before



def _manifest_16_fixture(tmp_path, monkeypatch, mutate=None):
    from scaffold_16 import scaffold_16
    from test_preparation_bundle import fixture
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_16)
    manifest = request["source"].manifest_path
    if mutate is not None:
        raw = json.loads(manifest.read_text())
        mutate(raw)
        manifest.write_text(json.dumps(raw))
    return bundle, root, request, manifest


@pytest.mark.parametrize("path", ["legacy", "connect"])
def test_manifest_16_is_refused_on_h1_and_legacy_before_any_effect(tmp_path, monkeypatch, path):
    """H1 and the legacy recipe stay /15-only: /16 gets their existing refusals, before any file is created."""
    from evolution_lab.client import ClientError, connect
    from evolution_lab.recipe import prepare_inputs

    bundle, root, request, manifest = _manifest_16_fixture(tmp_path, monkeypatch)
    before = sorted(tmp_path.rglob("*"))
    if path == "legacy":
        with pytest.raises(ValueError) as caught:
            prepare_inputs(manifest)
        assert str(caught.value) == "Lab requires a native campaign manifest: evolution-v2-manifest/15"
    else:
        with pytest.raises(ClientError) as caught:
            connect(manifest)
        assert caught.value.failure.public() == {"code": "manifest_invalid", "stage": "connect"}
    assert sorted(tmp_path.rglob("*")) == before


def _link(raw):
    raw["linked_from"] = {"run_dir": "../previous-run", "baseline": "fresh"}


def _agent_cli(raw):
    raw["grouper"] = {"kind": "agent_cli", "model": "codex", "timeout_seconds": 60}


@pytest.mark.parametrize("path", ["bundle", "form"])
@pytest.mark.parametrize("mutate, code", [(_link, "linked_run_unsupported"),
                                          (_agent_cli, "grouper_kind_unsupported")])
def test_manifest_16_lab_refusals_hold_on_owner_paths(tmp_path, monkeypatch, path, mutate, code):
    """linked_from and an agent_cli grouper are Lab capabilities, not Core ones: /16 is refused the same way."""
    from evolution_lab.preparation.models import PreparationError
    from evolution_lab.preparation.storage import PreparationStorage
    from evolution_lab.projects.package import read_package_entry, validate_form

    bundle, root, request, manifest = _manifest_16_fixture(tmp_path, monkeypatch, mutate)
    if path == "bundle":
        with pytest.raises(PreparationError) as caught:
            bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
        assert (caught.value.code, caught.value.stage) == (code, "bundle")
    else:
        entry = root / "bundle-request.json"
        entry.write_text(json.dumps({
            "source": {"kind": "campaign", "manifest_path": manifest.name, "launch_path": "launch.json"},
            "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
            "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
        errors = validate_form(read_package_entry(entry), place=root)
        assert code in {error["code"] for error in errors}


def test_lab_scaffold_writes_retry_campaign_table_after_strict_input_check():
    source = _b1()
    out = as_manifest_15(source)
    campaign = out["campaign"]
    assert campaign["sample_retries"] == 3
    assert campaign["call_retries"] == {"max_attempts": 3, "turn_max_attempts": 2}
    assert campaign["budget"] == {**source["campaign"]["budget"], "max_failed_turns": 2}
    assert set(campaign["indeterminate"]) == {"target", "environment", "scorer", "rollout"}
    assert all(owner == {"max_count": 20, "max_rate": "1"} for owner in campaign["indeterminate"].values())
    assert out["reflector"]["max_consecutive_failures"] == 5
    assert "sample_retries" not in source["campaign"]
    source["campaign"]["sample_retries"] = 3
    with pytest.raises(ValueError, match="campaign"):
        as_manifest_15(source)


def test_campaign_manifest_keeps_the_statistical_gate(tmp_path):
    from evolution.tree import scaffold
    from evolution_lab._scaffold_manifest import campaign_manifest
    from evolution_lab.quickstart import new_project

    project = new_project(tmp_path / "project")
    source = json.loads(scaffold(tmp_path / "reference").manifest.read_text())
    before = copy.deepcopy(source)
    out = campaign_manifest(source, project=project)
    assert source == before
    assert out["schema"] == "evolution-v2-manifest/15"
    assert out["campaign"]["gate"] == source["campaign"]["gate"]
    assert out["campaign"]["gate"]["mode"] == "statistical"
    for relative in (out["seed_root"], out["corpus"]["path"],
                     *(out[role]["cwd"] for role in ("scorer", "editor", "reflector"))):
        resolved = (project / relative).resolve()
        assert resolved.is_relative_to(project.resolve()) and resolved.exists()
    assert out["artifacts"]["dependency_paths"] == []
    assert out["scorer"]["argv"][0] == "lab:auto"


def test_campaign_project_is_generated_from_core(tmp_path):
    from evolution.tree import scaffold
    from evolution_lab._scaffold_manifest import campaign_manifest
    from evolution_lab.quickstart import new_project

    project = new_project(tmp_path / "project")
    reference = scaffold(tmp_path / "reference")
    source = json.loads(reference.manifest.read_text())
    assert json.loads((project / "manifest.json").read_text()) == campaign_manifest(source, project=project)
    for role, program in (("editor", "edit.py"), ("reflector", "reflect.py")):
        assert (project / role / program).read_bytes() == (reference.dir / role / program).read_bytes()
