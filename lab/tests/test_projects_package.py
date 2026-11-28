import json

import pytest

from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.package import covers_place, inputs_inventory, read_package_entry, validate_form


def _package(root, *, manifest="lab/manifest.json", launch="lab/launch.json", roots=None):
    (root / "lab").mkdir(parents=True, exist_ok=True)
    (root / "agent").mkdir(exist_ok=True)
    (root / "agent" / "a.py").write_text("A = 1\n")
    entry = root / "lab" / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": manifest.split("/", 1)[1], "launch_path": launch.split("/", 1)[1]},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": roots if roots is not None else [{"alias": "agent", "source_path": "../agent"}, {"alias": "lab", "source_path": "."}],
        "path_bindings": [],
    }))
    return entry


def test_relative_paths_resolve_from_the_entry(tmp_path):
    entry = _package(tmp_path)
    request = read_package_entry(entry)
    assert request.source.manifest_path == tmp_path / "lab" / "manifest.json"
    assert [root.source_path for root in request.roots] == [tmp_path / "agent", tmp_path / "lab"]


def test_entry_with_schema_is_refused(tmp_path):
    entry = _package(tmp_path)
    value = json.loads(entry.read_text())
    entry.write_text(json.dumps({"schema": "lab-service-bundle-request/2", **value}))
    with pytest.raises(ProjectError) as caught:
        read_package_entry(entry)
    assert caught.value.code == "package_unavailable"


def test_missing_entry_is_package_unavailable(tmp_path):
    with pytest.raises(ProjectError) as caught:
        read_package_entry(tmp_path / "none.json")
    assert caught.value.code == "package_unavailable"


def test_covers_place(tmp_path):
    request = read_package_entry(_package(tmp_path))
    assert covers_place(request, tmp_path / "agent")
    assert covers_place(request, tmp_path / "agent" / "sub")
    assert not covers_place(request, tmp_path / "other")


def test_inputs_inventory_is_stable_and_sees_changes(tmp_path):
    entry = _package(tmp_path)
    (tmp_path / "lab" / "manifest.json").write_text("{}")
    (tmp_path / "lab" / "launch.json").write_text("{}")
    request = read_package_entry(entry)
    digest, entries, sources = inputs_inventory(request, working_copy=tmp_path)
    assert {"alias": "agent", "path": "agent"} in sources
    again, _, _ = inputs_inventory(request, working_copy=tmp_path)
    (tmp_path / "agent" / "a.py").write_text("A = 2\n")
    changed, _, _ = inputs_inventory(request, working_copy=tmp_path)
    assert digest == again != changed


def test_validate_form_reports_problems_without_capture(tmp_path):
    entry = _package(tmp_path)
    errors = validate_form(read_package_entry(entry), place=tmp_path / "agent")
    assert {error["code"] for error in errors} >= {"unsupported_input"}  # manifest/launch missing
    outside = _package(tmp_path, roots=[{"alias": "lab", "source_path": "."}])
    errors = validate_form(read_package_entry(outside), place=tmp_path / "agent")
    assert any(error["code"] == "package_mismatch" for error in errors)


@pytest.mark.parametrize("launch_schema", ["lab-target-launch/2", "lab-target-launch/3"])
def test_validate_form_accepts_scaffold_package(tmp_path, launch_schema):
    from scaffold_15 import scaffold_15
    sample = scaffold_15(tmp_path / "lab")
    (tmp_path / "lab" / "launch.json").write_text(json.dumps({"schema": launch_schema}))
    entry = tmp_path / "lab" / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": sample.manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": [],
    }))
    assert validate_form(read_package_entry(entry), place=tmp_path / "lab") == []


def test_validate_form_refuses_manifest_13(tmp_path):
    from scaffold_15 import scaffold_15
    sample = scaffold_15(tmp_path / "lab")
    raw = json.loads(sample.manifest.read_text()); raw["schema"] = "evolution-v2-manifest/13"
    sample.manifest.write_text(json.dumps(raw))
    (tmp_path / "lab" / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = tmp_path / "lab" / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": sample.manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    assert {"code": "unsupported_schema", "detail": "manifest schema is not the pinned core's"} in \
        validate_form(read_package_entry(entry), place=tmp_path / "lab")


def _form_16(tmp_path, **campaign):
    from scaffold_16 import scaffold_16
    sample = scaffold_16(tmp_path / "lab")
    if campaign:
        raw = json.loads(sample.manifest.read_text()); raw["campaign"].update(campaign)
        sample.manifest.write_text(json.dumps(raw))
    (tmp_path / "lab" / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = tmp_path / "lab" / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": sample.manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    return validate_form(read_package_entry(entry), place=tmp_path / "lab")


def test_validate_form_accepts_manifest_16(tmp_path):
    assert _form_16(tmp_path) == []


def test_validate_form_16_promotion_is_manifest_invalid(tmp_path):
    errors = _form_16(tmp_path, promotion="manual")
    assert {"code": "manifest_invalid", "detail": "load_manifest_v2 refused the manifest"} in errors


@pytest.mark.parametrize("schema", [[], {}])
def test_validate_form_non_string_manifest_schema_is_unsupported(tmp_path, schema):
    from scaffold_15 import scaffold_15
    sample = scaffold_15(tmp_path / "lab")
    raw = json.loads(sample.manifest.read_text()); raw["schema"] = schema
    sample.manifest.write_text(json.dumps(raw))
    (tmp_path / "lab" / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = tmp_path / "lab" / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": sample.manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    assert {"code": "unsupported_schema", "detail": "manifest schema is not the pinned core's"} in \
        validate_form(read_package_entry(entry), place=tmp_path / "lab")


@pytest.mark.parametrize("role", [0, 1])
def test_environment_inventory_preserves_role_and_order_without_interpreter_bytes(tmp_path, role):
    from dataclasses import replace
    from evolution_lab.preparation import EnvironmentInput
    entry = _package(tmp_path / "work")
    request = read_package_entry(entry)
    executable = tmp_path / "python"
    executable.write_bytes(b"interpreter")
    first, second = tmp_path / "one.whl", tmp_path / "two.whl"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    # A document may simultaneously be a runtime file; both source roles survive.
    document = tmp_path / "work" / "lab" / "manifest.json"
    document.write_text("{}")
    environments = [EnvironmentInput(executable, (), ()), EnvironmentInput(executable, (), ())]
    environments[role] = EnvironmentInput(executable, (first, second), (document,))
    request = replace(request, environments=tuple(environments))
    original, entries, sources = inputs_inventory(request, working_copy=tmp_path / "work")
    assert {"path": f"@{first}"} in sources and {"path": f"@{second}"} in sources
    assert {"path": f"@{document}"} in sources and {"path": "lab/manifest.json"} in sources
    assert f"@{executable}" not in {e.path for e in entries}
    executable.write_bytes(b"different interpreter bytes")
    assert inputs_inventory(request, working_copy=tmp_path / "work")[0] == original
    environments[role] = replace(environments[role], wheel_paths=(second, first))
    assert inputs_inventory(replace(request, environments=tuple(environments)), working_copy=tmp_path / "work")[0] != original
    assert inputs_inventory(replace(request, environments=tuple(reversed(request.environments))),
                            working_copy=tmp_path / "work")[0] != original
