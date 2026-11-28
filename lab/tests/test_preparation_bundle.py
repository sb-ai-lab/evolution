"""V-S1-3: neutral saved bytes and schema-aware references, without execution."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from evolution.tree import load_manifest_v2
from scaffold_15 import scaffold_15
from scaffold_16 import scaffold_16

from evolution_lab.preparation.environment import CapturedEnvironment
from evolution_lab.preparation.models import (
    CampaignSource, EnvironmentDescriptor, EnvironmentInput, PathBinding,
    PreparationError, SourceRoot,
)
from evolution_lab.preparation.storage import PreparationStorage


def fixture(tmp_path, monkeypatch, scaffold=scaffold_15):
    from evolution_lab.preparation import bundle

    root = tmp_path / "source"
    scaffold(root)
    descriptor = EnvironmentDescriptor.from_payload({
        "schema": "lab-environment/1", "executable": sys.executable,
        "executable_sha256": "a" * 64, "python_version": "3.12.11",
        "distributions": [
            {"name": "evolution-lab", "version": "0.0.3.dev4", "wheel_sha256": "b" * 64},
            {"name": "evo-skills", "version": "0.3.0a5", "wheel_sha256": "c" * 64},
        ],
        "runtime_files": [], "platform": {"system": "Linux", "machine": "x86_64"},
        "proof_profile": "wheel-members/1", "limitations": ["synthetic capture boundary"],
    })
    # Task2 owns installed-wheel proof. Source tests isolate that boundary only;
    # all copying, loaders, relocation, inventory and publication remain real.
    def capture(value, *, execution):
        assert value.executable == Path(sys.executable)
        assert execution is True
        return CapturedEnvironment(descriptor, '{"archive_wheels":[]}')
    monkeypatch.setattr(bundle, "capture_environment", capture)
    manifest = root / "manifest.json"
    raw = json.loads(manifest.read_text())
    for role in ("scorer", "editor", "reflector"):
        raw[role]["argv"][0] = sys.executable
    manifest.write_text(json.dumps(raw))
    (root / "empty").mkdir()
    (root / "launcher.py").write_text("raise RuntimeError('must not execute')\n")
    (root / "launcher.py").chmod(0o751)
    os.link(root / "launcher.py", root / "launcher-copy.py")
    launch = root / "launch.json"
    launch.write_text(json.dumps({
        "schema": "lab-target-launch/1",
        "command": {"executable": sys.executable, "args": [{"bundle_path": "launcher.py"}, {"literal": "--offline"}]},
        "environment_ref": descriptor.digest, "program_paths": ["launcher.py"],
        "env": {"MODE": "local"}, "secret_names": ["TOKEN"],
        "endpoint": {"base_url": raw["target"]["base_url"]},
        "identity": {"target_id": "neutral", "contract_digest": None,
                     "settings_hash": None, "runtime_ref_digest": None,
                     "seed_mode": raw["target"]["seed_mode"]},
        "policy": "manifest.json", "limits": {"max_instances": 4, "max_concurrent_sessions": 1,
                                                  "max_input_bytes": 4096, "max_output_bytes": 4096},
        "startup_timeout_seconds": 10, "stop_grace_seconds": 10, "conformance": None,
    }))
    request = dict(source=CampaignSource(manifest, launch),
                   environments=(EnvironmentInput(Path(sys.executable), (tmp_path / "retained.whl",)),),
                   roots=(SourceRoot("project", root),), path_bindings=(),
                   env={"MODE": "local"}, secret_names=("TOKEN",))
    return bundle, root, request


def test_bundle_skips_git_and_python_caches(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    plain = bundle.create_bundle(PreparationStorage(tmp_path / "state-a"), **request)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "m.cpython-314.pyc").write_bytes(b"\x00")
    (root / "loose.pyc").write_bytes(b"\x00")
    (root / "pkg" / "m.py").write_text("X = 1\n")
    noisy = bundle.create_bundle(PreparationStorage(tmp_path / "state-b"), **request)
    paths = [entry.path for entry in noisy.descriptor.entries]
    assert not any(".git" in path or "__pycache__" in path or path.endswith(".pyc") for path in paths)
    assert any(path.endswith("pkg/m.py") for path in paths)
    assert noisy.digest != plain.digest  # only because pkg/m.py was added


def test_bundle_skips_a_dot_venv_with_symlinks_to_the_system_python(tmp_path, monkeypatch):
    """A test environment left in the place (``bin/python`` and ``lib64`` are symlinks) never enters
    the Bundle, so it is not refused as ``unsupported_input`` and the digest stays as without it."""
    import sys
    bundle, root, request = fixture(tmp_path, monkeypatch)
    plain = bundle.create_bundle(PreparationStorage(tmp_path / "state-a"), **request)
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    (venv / "lib").mkdir()
    (venv / "lib64").symlink_to(venv / "lib", target_is_directory=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    with_venv = bundle.create_bundle(PreparationStorage(tmp_path / "state-b"), **request)
    assert not any(".venv" in entry.path for entry in with_venv.descriptor.entries)
    assert with_venv.digest == plain.digest

def test_bundle_refuses_documents_inside_excluded_paths(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    # Keep every reference the moved launch document makes resolvable — place copies of
    # manifest.json and launcher.py (unchanged field values: "manifest.json"/"launcher.py",
    # local_path() rejects ".." components outright) alongside it inside .git — so the
    # pre-existing reference/existence checks inside Relocator.launch() cannot themselves
    # raise unsupported_input here; only the excluded_path guard on the launch document's
    # own location can refuse this bundle.
    hidden = root / ".git" / "launch.json"
    hidden.parent.mkdir()
    hidden.write_bytes(request["source"].launch_path.read_bytes())
    shutil.copy(request["source"].manifest_path, root / ".git" / "manifest.json")
    shutil.copy(root / "launcher.py", root / ".git" / "launcher.py")
    request["source"] = type(request["source"])(request["source"].manifest_path, hidden)
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "unsupported_input"


def test_bundle_refuses_path_binding_relative_path_inside_excluded_paths(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    # The bound target physically exists and matches the binding exactly, so the
    # pre-existing bound-vs-source and existence checks in Relocator.reference() cannot
    # themselves raise unsupported_input here — only the excluded_path guard on the
    # binding's relative_path (and the referring document's logical path) can refuse it.
    (root / ".git").mkdir()
    (root / ".git" / "score.py").write_text("X = 1\n")
    raw = json.loads(request["source"].manifest_path.read_text())
    raw["scorer"]["argv"][1] = str(root / ".git" / "score.py")
    request["source"].manifest_path.write_text(json.dumps(raw))
    request["path_bindings"] = (
        PathBinding(request["source"].manifest_path, "/scorer/argv/1", "project", ".git/score.py"),
    )
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "unsupported_input"


def _moved_root(tmp_path, monkeypatch, destination):
    """The fixture's tree moved to ``destination`` (relative references stay valid)."""
    bundle, root, request = fixture(tmp_path, monkeypatch)
    destination.parent.mkdir(parents=True, exist_ok=True)
    moved = root.rename(destination)
    request["source"] = CampaignSource(moved / "manifest.json", moved / "launch.json")
    request["roots"] = (SourceRoot("project", moved),)
    return bundle, request


@pytest.mark.parametrize("location", ["root_ancestor", "nested"])
def test_symlink_ancestor_refusal_names_root_and_path(tmp_path, monkeypatch, location):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    if location == "root_ancestor":
        parent = tmp_path / "linked-parent"
        parent.symlink_to(tmp_path, target_is_directory=True)
        linked_root = parent / root.name
        request["source"] = CampaignSource(linked_root / "manifest.json", linked_root / "launch.json")
        request["roots"] = (SourceRoot("project", linked_root),)
        logical = "declared root 'project' path passes through a symbolic link"
    else:
        (root / "linked").symlink_to(root / "seed", target_is_directory=True)
        logical = "project/linked"
    storage = PreparationStorage(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"
    message = str(caught.value)
    if location == "nested":
        assert "symlink ancestor" in message
    assert logical in message
    assert str(tmp_path) not in message
    assert not list((storage.state_directory / "bundles").iterdir())


def test_read_file_symlink_refusal_names_logical_path(tmp_path, monkeypatch):
    from evolution_lab.preparation import bundle

    root = tmp_path / "source"
    root.mkdir()
    path = root / "input.txt"
    path.write_text("original")
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    safe_info = bundle._safe_info

    def replace_after_inspection(candidate, **kwargs):
        info = safe_info(candidate, **kwargs)
        if candidate == path:
            path.unlink()
            path.symlink_to(outside)
        return info

    monkeypatch.setattr(bundle, "_safe_info", replace_after_inspection)
    with pytest.raises(PreparationError) as caught:
        bundle._snapshot({"project": root})

    assert caught.value.failure.to_payload() == {
        "code": "unsupported_input", "stage": "bundle",
        "message": "A declared preparation input is not supported. symlink ancestor: 'project/input.txt'",
    }
    assert str(tmp_path) not in str(caught.value)


def test_evaluation_bundle_policy_refusal_names_field(tmp_path, monkeypatch):
    from evolution_lab.preparation import bundle
    from test_preparation_evaluation import _evaluation_request

    request = _evaluation_request(tmp_path, monkeypatch)
    launch = request["source"].launch_path
    raw = json.loads(launch.read_text())
    raw["policy"] = "manifest.json"
    launch.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"
    assert "launch.policy" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert not list((storage.state_directory / "bundles").iterdir())


@pytest.mark.parametrize("relative", [
    "repo/.git", "repo/__pycache__",  # the root's own name is excluded
    "repo/.git/source",               # the root lies inside an excluded entry
])
def test_bundle_refuses_a_root_that_is_or_lies_inside_an_excluded_entry(tmp_path, monkeypatch, relative):
    """ADR-0017 p. 6 at the top-level boundary (codex r2): relocation hides the root's own
    physical name, so filtering only its children let ``<repo>/.git`` be copied whole."""
    bundle, request = _moved_root(tmp_path, monkeypatch, tmp_path / relative)
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "unsupported_input"


def test_bundle_accepts_the_same_moved_root_under_a_plain_name(tmp_path, monkeypatch):
    """Control for the refusal above: moving the tree is not what refuses it."""
    bundle, request = _moved_root(tmp_path, monkeypatch, tmp_path / "repo" / "gitdir")
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert any(entry.path == "inputs/project/manifest.json" for entry in stored.descriptor.entries)


def test_bundle_relocates_command_bases_and_retains_bytes_after_source_deletion(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = json.loads(request["source"].manifest_path.read_text())
    for role, filename in (("scorer", "score.py"), ("editor", "edit.py")):
        raw[role]["cwd"] = str(root / role)
        raw[role]["argv"][1] = str(root / role / filename)
    request["source"].manifest_path.write_text(json.dumps(raw))
    request["path_bindings"] = tuple(
        PathBinding(request["source"].manifest_path, f"/{role}/argv/1", "project", f"{role}/{filename}")
        for role, filename in (("scorer", "score.py"), ("editor", "edit.py"))
    )
    storage = PreparationStorage(tmp_path / "state")
    stored = bundle.create_bundle(storage, **request)
    manifest = load_manifest_v2(stored.path / stored.descriptor.entrypoint)
    assert manifest.workflow.scorer.cwd == stored.path / "inputs/project/scorer"
    assert manifest.workflow.scorer.argv[1] == "score.py"
    assert manifest.workflow.editor.argv[1] == "edit.py"
    assert manifest.seed_root == stored.path / "inputs/project/seed"
    launch = json.loads((stored.path / stored.descriptor.launch).read_bytes())
    assert launch["command"]["args"] == [{"bundle_path": "inputs/project/launcher.py"}, {"literal": "--offline"}]
    assert launch["policy"] == stored.descriptor.entrypoint
    entries = {entry.path: entry for entry in stored.descriptor.entries}
    assert entries["inputs/project/empty"].type == "directory"
    assert entries["inputs/project/launcher.py"].executable == 0o111
    a, b = (stored.path / f"inputs/project/{name}" for name in ("launcher.py", "launcher-copy.py"))
    assert a.stat().st_ino != b.stat().st_ino
    original = a.read_bytes()
    assert entries["inputs/project/launcher.py"].sha256 == hashlib.sha256(original).hexdigest()
    receipt = storage.state_directory / "receipts/bundles" / f"{stored.digest}.json"
    assert str(root) in receipt.read_text()
    assert str(root) not in (stored.path / stored.descriptor.entrypoint).read_text()
    shutil.rmtree(root)
    reopened = PreparationStorage(storage.state_directory).get_bundle(stored.digest)
    assert reopened.digest == stored.digest
    assert a.read_bytes() == original


def test_bundle_relocates_nested_grouping_and_keeps_opaque_config_literal(
    tmp_path, monkeypatch
):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    grouping = root / "grouping"
    (grouping / "nested").mkdir(parents=True)
    (grouping / "config").mkdir()
    script = grouping / "nested/group.py"
    argument = grouping / "config/request.json"
    dependency = root / "shared/grouping.txt"
    dependency.parent.mkdir()
    script.write_text("raise RuntimeError('must not execute')\n")
    argument.write_text('{"kind":"neutral"}\n')
    dependency.write_text("grouping dependency\n")
    docs = root / "docs"
    (docs / "nested").mkdir(parents=True)
    (docs / "guide.md").write_text("guide\n")
    (docs / "nested/details.md").write_text("details\n")
    opaque_locator = str(dependency)

    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["reflector"]["environment_docs"] = "docs"
    raw["grouper"] = {
        "kind": "command",
        "argv": [sys.executable, "nested/group.py", "config/request.json"],
        "cwd": "grouping",
        "dependency_paths": ["shared/grouping.txt"],
        "allowed_env": [],
        "timeout_seconds": 17,
        "config": {
            "opaque_locator": opaque_locator,
            "nested": {"path": "config/request.json"},
        },
    }
    path.write_text(json.dumps(raw))

    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    saved = json.loads((stored.path / stored.descriptor.entrypoint).read_bytes())
    manifest = load_manifest_v2(stored.path / stored.descriptor.entrypoint)
    command = manifest.workflow.grouper.command

    assert command.cwd == stored.path / "inputs/project/grouping"
    assert command.argv == (
        sys.executable,
        "nested/group.py",
        "config/request.json",
    )
    assert command.dependency_paths == (
        stored.path / "inputs/project/shared/grouping.txt",
    )
    assert saved["grouper"]["config"] == {
        "opaque_locator": opaque_locator,
        "nested": {"path": "config/request.json"},
    }
    entries = {entry.path for entry in stored.descriptor.entries}
    assert {
        "inputs/project/grouping/nested/group.py",
        "inputs/project/grouping/config/request.json",
        "inputs/project/shared/grouping.txt",
        "inputs/project/docs/guide.md",
        "inputs/project/docs/nested/details.md",
    } <= entries

    shutil.rmtree(root)
    reopened = load_manifest_v2(stored.path / stored.descriptor.entrypoint)
    assert reopened.workflow.grouper.command.argv[1:] == (
        "nested/group.py",
        "config/request.json",
    )
    assert (stored.path / "inputs/project/docs/nested/details.md").read_text() == "details\n"


def test_bundle_relocates_model_grouping_system_prompt(tmp_path, monkeypatch):
    from test_owner_binding import MODEL_GROUPING

    bundle, root, request = fixture(tmp_path, monkeypatch)
    (root / "grouping.md").write_bytes(b"group the failures by cause\n")
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["grouper"] = {**MODEL_GROUPING, "system_prompt": "grouping.md"}
    path.write_text(json.dumps(raw))

    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    saved = json.loads((stored.path / stored.descriptor.entrypoint).read_bytes())
    grouping = load_manifest_v2(stored.path / stored.descriptor.entrypoint).workflow.grouper

    assert grouping.system_prompt == stored.path / "inputs/project/grouping.md"
    assert grouping.system_prompt.read_bytes() == b"group the failures by cause\n"
    # Saved relative to the Bundle manifest, which sits in the same `inputs/project` directory.
    assert saved["grouper"]["system_prompt"] == "grouping.md"
    assert "inputs/project/grouping.md" in {entry.path for entry in stored.descriptor.entries}
    shutil.rmtree(root)
    assert grouping.system_prompt.read_bytes() == b"group the failures by cause\n"


def test_bundle_keeps_model_grouping_without_system_prompt(tmp_path, monkeypatch):
    from test_owner_binding import MODEL_GROUPING

    bundle, root, request = fixture(tmp_path, monkeypatch)
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["grouper"] = dict(MODEL_GROUPING)
    path.write_text(json.dumps(raw))

    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    grouping = load_manifest_v2(stored.path / stored.descriptor.entrypoint).workflow.grouper

    assert grouping.system_prompt is None
    assert grouping.api_key_env == "GROUPING_KEY" and grouping.proxy_env == "GROUPING_PROXY"


def test_grouping_known_attached_path_operand_is_unsupported(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    grouping = root / "grouping"
    grouping.mkdir()
    (grouping / "group.py").write_text("pass\n")
    (grouping / "config.json").write_text("{}\n")
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["grouper"] = {
        "kind": "command",
        "argv": [sys.executable, "group.py", "--config=config.json"],
        "cwd": "grouping",
        "dependency_paths": [],
        "allowed_env": [],
        "timeout_seconds": 17,
        "config": {},
    }
    path.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"
    assert not list((storage.state_directory / "bundles").iterdir())


@pytest.mark.parametrize("invalid", ["empty", "non_utf8"])
def test_bundle_refuses_invalid_complete_docs_tree(tmp_path, monkeypatch, invalid):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    docs = root / "docs"
    docs.mkdir()
    if invalid == "non_utf8":
        (docs / "bad.bin").write_bytes(b"\xff")
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["reflector"]["environment_docs"] = "docs"
    path.write_text(json.dumps(raw))

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"


@pytest.mark.parametrize("failure", ["symlink", "traversal", "unknown_binding", "outside_root"])
def test_unsupported_declared_paths_never_publish_or_fall_back(tmp_path, monkeypatch, failure):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    raw = json.loads(request["source"].manifest_path.read_text())
    if failure == "symlink":
        (root / "link").symlink_to(root / "launcher.py")
    elif failure == "traversal":
        raw["seed_root"] = "../source/seed"
    elif failure == "outside_root":
        outside = tmp_path / "outside.jsonl"
        outside.write_text("{}\n")
        raw["corpus"]["path"] = str(outside)
    else:
        request["path_bindings"] = (PathBinding(request["source"].manifest_path, "/target/base_url", "project", "launcher.py"),)
    request["source"].manifest_path.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert caught.value.code == "unsupported_input"
    assert not list((storage.state_directory / "bundles").iterdir())


def test_detected_source_deletion_during_copy_is_inputs_changed_and_discards_stage(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    storage = PreparationStorage(tmp_path / "state")
    read = bundle._read_file
    removed = False

    def remove_during_copy(path, **kwargs):
        nonlocal removed
        if path == root / "launcher.py" and not removed and list((storage.state_directory / "bundles").iterdir()):
            removed = True
            path.unlink()
        return read(path, **kwargs)

    monkeypatch.setattr(bundle, "_read_file", remove_during_copy)
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert caught.value.code == "inputs_changed"
    assert removed
    assert not list((storage.state_directory / "bundles").iterdir())


@pytest.mark.parametrize("failure,code", [
    ("native_unknown", "manifest_invalid"), ("native_old", "unsupported_schema"),
    ("native_linked", "linked_run_unsupported"), ("grouper_agent_cli", "grouper_kind_unsupported"),
    ("fifo", "unsupported_input"), ("state_overlap", "unsupported_input"),
    ("wheel_overlap", "unsupported_input"), ("binding_changes_operand", "unsupported_input"),
    ("launch_environment", "environment_invalid"), ("secret_subset", "invalid_request"),
    ("duplicate_json", "manifest_invalid"), ("corpus_in_scorer", "corpus_inside_role_workspace"),
])
def test_invalid_inputs_fail_with_closed_codes(tmp_path, monkeypatch, failure, code):
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    storage = PreparationStorage(tmp_path / "state")
    manifest = request["source"].manifest_path
    raw = json.loads(manifest.read_text())
    if failure == "native_unknown":
        raw["custom_campaign_override"] = True
    elif failure == "native_old":
        raw["schema"] = "evolution-v2-manifest/3"
    elif failure == "native_linked":
        raw["linked_from"] = {"run_dir": "../previous-run", "baseline": "fresh"}
    elif failure == "grouper_agent_cli":
        from evolution.tree import load_manifest_v2 as _core_loads
        raw["grouper"] = {"kind": "agent_cli", "model": "codex", "timeout_seconds": 60}
        manifest.write_text(json.dumps(raw))
        assert _core_loads(manifest).workflow.grouper is not None  # core accepts it: the refusal is Lab's
    elif failure == "corpus_in_scorer":
        shutil.move(root / raw["corpus"]["path"], root / raw["scorer"]["cwd"] / "corpus.jsonl")
        raw["corpus"]["path"] = f"{raw['scorer']['cwd']}/corpus.jsonl"
    elif failure == "fifo":
        os.mkfifo(root / "pipe")
    elif failure == "state_overlap":
        request["roots"] = (*request["roots"], SourceRoot("state", storage.state_directory))
    elif failure == "wheel_overlap":
        request["environments"] = (EnvironmentInput(Path(sys.executable), (root / "retained.whl",)),)
    elif failure == "binding_changes_operand":
        request["path_bindings"] = (PathBinding(manifest, "/corpus/path", "project", "launcher.py"),)
    elif failure in {"launch_environment", "secret_subset"}:
        launch_path = request["source"].launch_path
        launch = json.loads(launch_path.read_text())
        launch["environment_ref" if failure == "launch_environment" else "secret_names"] = "f" * 64 if failure == "launch_environment" else ["UNDECLARED"]
        launch_path.write_text(json.dumps(launch))
    manifest.write_text(json.dumps(raw))
    if failure == "duplicate_json":
        manifest.write_text(manifest.read_text().replace('"seed_root":', '"seed_root":"ignored", "seed_root":'))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert caught.value.code == code
    assert caught.value.stage == "bundle"
    assert not list((storage.state_directory / "bundles").iterdir())


@pytest.mark.parametrize("schema", ["evolution-v2-manifest/8", "evolution-v2-manifest/12",
                                    "evolution-v2-manifest/13", "evolution-v2-manifest/14"])
def test_lab_admits_15_and_16_and_refuses_others(tmp_path, monkeypatch, schema):
    """PDR-0015: /15 and /16 are admitted on the Owner paths; /8, /12, /13 and /14 stay refused."""
    from evolution_lab._manifest_schema import (
        CAMPAIGN_MANIFEST_SCHEMA, CAMPAIGN_MANIFEST_SCHEMAS, LEGACY_CAMPAIGN_MANIFEST_SCHEMAS)
    assert CAMPAIGN_MANIFEST_SCHEMAS == {"evolution-v2-manifest/15", "evolution-v2-manifest/16"}
    assert LEGACY_CAMPAIGN_MANIFEST_SCHEMAS == {"evolution-v2-manifest/15"}
    assert CAMPAIGN_MANIFEST_SCHEMA == "evolution-v2-manifest/15"
    bundle, root, request = fixture(tmp_path, monkeypatch)
    storage = PreparationStorage(tmp_path / "state")
    manifest = request["source"].manifest_path
    raw = json.loads(manifest.read_text())
    raw["schema"] = schema
    manifest.write_text(json.dumps(raw))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert caught.value.code == "unsupported_schema"
    assert not list((storage.state_directory / "bundles").iterdir())


def test_manifest_16_creates_a_bundle(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_16)
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert load_manifest_v2(stored.path / stored.descriptor.entrypoint).schema == "evolution-v2-manifest/16"


@pytest.mark.parametrize("value", ["manual", "automatic"])
def test_manifest_16_promotion_key_is_cores_refusal(tmp_path, monkeypatch, value):
    """b8 removed manual promotion: the key itself is unknown, and Lab carries Core's refusal unchanged."""
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_16)
    manifest = request["source"].manifest_path
    raw = json.loads(manifest.read_text())
    raw["campaign"]["promotion"] = value
    manifest.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert (caught.value.code, caught.value.stage) == ("manifest_invalid", "bundle")
    assert not list((storage.state_directory / "bundles").iterdir())


def test_manifest_15_creates_a_bundle_with_the_top_level_grouper(tmp_path, monkeypatch):
    """Spec 2026-10-13 §5.1: /15 keeps the Grouper at the top level (kind fields), `reflector` has no `grouping`."""
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    source = json.loads(request["source"].manifest_path.read_text())
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    entry = stored.path / stored.descriptor.entrypoint
    assert load_manifest_v2(entry).schema == "evolution-v2-manifest/15"
    saved = json.loads(entry.read_bytes())
    assert saved["grouper"] == source["grouper"] and saved["grouper"]["kind"] == "fields"
    assert "grouping" not in saved["reflector"]


def test_manifest_15_without_gate_mode_carries_core_diagnosis(tmp_path, monkeypatch):
    """Spec 2026-10-09 §3 (codex r1): core refuses the gate before any launch; Lab's code stays manifest_invalid
    and its message carries core's key path, code and `expected …` phrase."""
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    manifest = request["source"].manifest_path
    raw = json.loads(manifest.read_text())
    del raw["campaign"]["gate"]["mode"]
    manifest.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert (caught.value.code, caught.value.stage) == ("manifest_invalid", "bundle")
    assert caught.value.failure.message == ("The campaign manifest is invalid. missing_key at campaign.gate.mode: "
                                            "expected exactly 'statistical' or 'observe'")
    assert str(caught.value) == caught.value.failure.message
    assert not list((storage.state_directory / "bundles").iterdir())


def test_declared_extra_file_and_reflector_docs_are_copied_without_sibling_inputs(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "environment.md").write_text("neutral documentation")
    extra = tmp_path / "extra.txt"
    extra.write_text("explicit extra input")
    (tmp_path / "private.txt").write_text("not declared")
    raw = json.loads(request["source"].manifest_path.read_text())
    raw["reflector"]["environment_docs"] = str(docs)
    request["source"].manifest_path.write_text(json.dumps(raw))
    request["roots"] = (*request["roots"], SourceRoot("docs", docs), SourceRoot("extra", extra))
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    manifest = load_manifest_v2(stored.path / stored.descriptor.entrypoint)
    assert manifest.workflow.reflector.environment_docs == stored.path / "inputs/docs"
    assert (stored.path / "inputs/extra").read_text() == "explicit extra input"
    assert not any("private.txt" in entry.path for entry in stored.descriptor.entries)


def test_equivalent_source_locations_share_digest_but_keep_raw_manifest_hash_in_receipt(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path / "one", monkeypatch)
    raw_hash = hashlib.sha256(request["source"].manifest_path.read_bytes()).hexdigest()
    storage = PreparationStorage(tmp_path / "state")
    first = bundle.create_bundle(storage, **request)
    _, _, second_request = fixture(tmp_path / "two", monkeypatch)
    second = bundle.create_bundle(storage, **second_request)
    assert second.digest == first.digest
    receipt = json.loads((storage.state_directory / "receipts/bundles" / f"{first.digest}.json").read_bytes())
    assert receipt["manifest_sha256"] == raw_hash
    assert str(root) not in json.dumps(first.descriptor.to_payload())


def test_abort_cannot_discard_foreign_or_reused_staging(tmp_path):
    first = PreparationStorage(tmp_path / "state")
    other = PreparationStorage(tmp_path / "state")
    stage = first.create_bundle_staging()
    with pytest.raises(PreparationError):
        other.abort_bundle_staging(stage)
    assert stage.is_dir()
    first.abort_bundle_staging(stage)
    assert not stage.exists()
    with pytest.raises(PreparationError):
        first.abort_bundle_staging(stage)


def test_execution_environment_must_be_the_first_declared_environment(tmp_path, monkeypatch):
    bundle, _, request = fixture(tmp_path, monkeypatch)
    request["environments"] = (EnvironmentInput(Path("/opt/target/python"), (tmp_path / "target.whl",)), *request["environments"])
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "environment_invalid"


@pytest.mark.parametrize("field,value", [
    ("limits", {"max_instances": 1, "unsupported_limit": 2}),
    ("limits", {"max_instances": True, "max_concurrent_sessions": 1, "max_input_bytes": 20, "max_output_bytes": 20}),
    ("endpoint", {"base_url": 42}),
    ("identity", {"target_id": "neutral"}),
    ("conformance", {"kind": "unknown"}),
    ("startup_timeout_seconds", 0),
])
def test_launch_declaration_rejects_unsupported_nested_shapes(tmp_path, monkeypatch, field, value):
    bundle, _, request = fixture(tmp_path, monkeypatch)
    path = request["source"].launch_path
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "unsupported_input"


def test_drift_during_environment_capture_takes_precedence_over_stale_input_error(tmp_path, monkeypatch):
    bundle, _, request = fixture(tmp_path, monkeypatch)

    def mutate_then_fail(value, *, execution):
        request["source"].manifest_path.write_text("broken source")
        raise PreparationError("environment_invalid", "bundle")

    monkeypatch.setattr(bundle, "capture_environment", mutate_then_fail)
    storage = PreparationStorage(tmp_path / "state")
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert caught.value.code == "inputs_changed"
    assert not list((storage.state_directory / "bundles").iterdir())


def test_distinct_target_environment_is_selected_by_digest_and_chat_prompt_is_relocated(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    from dataclasses import replace

    execution_capture = bundle.capture_environment(request["environments"][0], execution=True)
    target_descriptor = replace(execution_capture.descriptor, executable="/opt/neutral-target/python")
    target_capture = CapturedEnvironment(target_descriptor, '{"archive_wheels":[]}')

    def capture(value, *, execution):
        if value.executable == Path(sys.executable):
            assert execution is True
            return execution_capture
        assert value.executable == Path("/opt/neutral-target/python")
        assert execution is False
        return target_capture

    monkeypatch.setattr(bundle, "capture_environment", capture)
    request["environments"] = (*request["environments"], EnvironmentInput(Path(target_descriptor.executable), (tmp_path / "target.whl",)))
    launch_path = request["source"].launch_path
    launch = json.loads(launch_path.read_text())
    launch["environment_ref"] = target_descriptor.digest
    launch["command"]["executable"] = target_descriptor.executable
    launch_path.write_text(json.dumps(launch))
    (root / "prompt.txt").write_text("neutral prompt")
    manifest_path = request["source"].manifest_path
    raw = json.loads(manifest_path.read_text())
    raw["editor"] = {"kind": "chat", "model": "neutral",
                     "context_compact_chars": 128_000, "context_message_max_chars": 12_000, "base_url": "http://127.0.0.1:9000/v1",
                     "api_key_env": "TOKEN", "system_prompt": str(root / "prompt.txt"),
                     # /15 requires the author's own retry on a chat role; Lab never supplies it (PDR-0015).
                     "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 60,
                               "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 8,
                                           "jitter": "full"}}}
    manifest_path.write_text(json.dumps(raw))
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert stored.descriptor.environments[0].digest == execution_capture.descriptor.digest
    saved_launch = json.loads((stored.path / stored.descriptor.launch).read_bytes())
    assert saved_launch["environment_ref"] == stored.descriptor.environments[1].digest
    assert saved_launch["command"]["executable"] == "/opt/neutral-target/python"
    manifest = load_manifest_v2(stored.path / stored.descriptor.entrypoint)
    assert manifest.workflow.editor.system_prompt.read_text() == "neutral prompt"
    assert manifest.workflow.editor.system_prompt == stored.path / "inputs/project/prompt.txt"


@pytest.mark.parametrize("role,form", [
    ("scorer", "absolute"),
    ("editor", "relative"),
    ("reflector", "basename"),
    ("scorer", "missing_relative"),
])
def test_attached_option_file_operands_are_unsupported_without_publication(tmp_path, monkeypatch, role, form):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    config = root / role / "cfg.json"
    config.write_text('{"mode":"neutral"}')
    operand = {
        "absolute": str(config), "relative": "./cfg.json",
        "basename": "cfg.json", "missing_relative": "config/missing.json",
    }[form]
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw[role]["cwd"] = str(root / role)
    raw[role]["argv"].append(f"--config={operand}")
    path.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"
    assert not list((storage.state_directory / "bundles").iterdir())


def test_attached_literal_options_and_opaque_code_and_env_are_not_path_scanned(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    code = f"print({str(root)!r})"
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["scorer"]["argv"] = [sys.executable, "-I", "-S", "-u", "-c", code, "--label=neutral"]
    path.write_text(json.dumps(raw))
    request["env"] = {**request["env"], "SOURCE_DESCRIPTION": f"source originally in {root}"}

    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)

    saved = json.loads((stored.path / stored.descriptor.entrypoint).read_bytes())
    assert saved["scorer"]["argv"] == [sys.executable, "-I", "-S", "-u", "-c", code, "--label=neutral"]
    assert stored.descriptor.env["SOURCE_DESCRIPTION"] == f"source originally in {root}"


@pytest.mark.parametrize("entry,flag", [("script", "-c"), ("script", "-m"), ("module", "-c"), ("code", "-m")])
def test_python_entrypoint_ends_interpreter_options_and_later_file_arguments_relocate(tmp_path, monkeypatch, entry, flag):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    config = root / "scorer/cfg.json"
    config.write_text("{}")
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["scorer"]["cwd"] = str(root / "scorer")
    program = {"script": ["score.py"], "module": ["-m", "neutral.module"], "code": ["-c", "pass"]}[entry]
    raw["scorer"]["argv"] = [sys.executable, *program, flag, str(config)]
    path.write_text(json.dumps(raw))
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    saved = json.loads((stored.path / stored.descriptor.entrypoint).read_bytes())
    assert saved["scorer"]["argv"] == [sys.executable, *program, flag, "cfg.json"]
    shutil.rmtree(root)
    assert (stored.path / "inputs/project/scorer/cfg.json").read_text() == "{}"


@pytest.mark.parametrize("form", ["absolute", "relative", "basename"])
def test_short_attached_path_arguments_are_unsupported(tmp_path, monkeypatch, form):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    config = root / "scorer/cfg.json"
    config.write_text("{}")
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["scorer"]["cwd"] = str(root / "scorer")
    operand = {"absolute": str(config), "relative": "./cfg.json", "basename": "cfg.json"}[form]
    raw["scorer"]["argv"].append(f"-C{operand}")
    path.write_text(json.dumps(raw))
    storage = PreparationStorage(tmp_path / "state")
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert caught.value.code == "unsupported_input"
    assert not list((storage.state_directory / "bundles").iterdir())


@pytest.mark.parametrize("prefix", [["--unknown"], ["-X", "dev"], ["-cprint('inline')"], ["-m", "bad/name"], ["-m", "bad..name"]])
def test_unknown_python_prefix_and_non_module_names_are_unsupported(tmp_path, monkeypatch, prefix):
    bundle, _, request = fixture(tmp_path, monkeypatch)
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["scorer"]["argv"] = [sys.executable, *prefix, *raw["scorer"]["argv"][1:]]
    path.write_text(json.dumps(raw))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "unsupported_input"


def test_launch_unhashable_schema_is_unsupported_schema(tmp_path, monkeypatch):
    # `schema not in _LAUNCH_KEYS` alone raises TypeError on an unhashable value
    # (a list) instead of the closed unsupported_schema code; guard with isinstance first.
    bundle, root, request = fixture(tmp_path, monkeypatch)
    launch_path = request["source"].launch_path
    launch_v1 = json.loads(launch_path.read_text())
    launch_v1["schema"] = ["x"]
    launch_path.write_text(json.dumps(launch_v1))

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)

    assert caught.value.code == "unsupported_schema"
    assert caught.value.stage == "bundle"


def launch_v2(launch_v1, **agent):
    document = {key: value for key, value in launch_v1.items() if key != "command"}
    document["schema"] = "lab-target-launch/2"
    document["agent"] = {"module": "echo_agent", "attribute": "build", "import_root": None, **agent}
    return document


def test_launch_v2_relocates_import_root_and_keeps_module(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    (root / "agent").mkdir()
    (root / "agent" / "echo_agent.py").write_text("def build():\n    pass\n")
    launch_path = request["source"].launch_path
    launch_v1 = json.loads(launch_path.read_text())
    launch_path.write_text(json.dumps(launch_v2(launch_v1, import_root="agent")))

    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)

    saved_launch = json.loads((stored.path / stored.descriptor.launch).read_bytes())
    assert saved_launch["agent"] == {
        "module": "echo_agent", "attribute": "build",
        "import_root": "inputs/project/agent",
    }
    assert "command" not in saved_launch


def test_launch_v2_accepts_a_dotted_module(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    (root / "agent" / "pkg").mkdir(parents=True)
    (root / "agent" / "pkg" / "__init__.py").write_text("")
    (root / "agent" / "pkg" / "entry.py").write_text("def build():\n    pass\n")
    launch_path = request["source"].launch_path
    launch_v1 = json.loads(launch_path.read_text())
    launch_path.write_text(json.dumps(launch_v2(launch_v1, module="pkg.entry", import_root="agent")))

    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)

    saved_launch = json.loads((stored.path / stored.descriptor.launch).read_bytes())
    assert saved_launch["agent"] == {
        "module": "pkg.entry", "attribute": "build",
        "import_root": "inputs/project/agent",
    }


@pytest.mark.parametrize("agent,expected_code", [
    # Shape errors: rejected by the agent-shape check in Relocator.launch() -> unsupported().
    ({"module": "1bad", "attribute": "build", "import_root": None}, "unsupported_input"),
    ({"module": "ok.mod", "attribute": "not-ident", "import_root": None}, "unsupported_input"),
    ({"module": "ok", "attribute": "build"}, "unsupported_input"),
    ({"module": "ok", "attribute": "build", "import_root": None, "extra": 1}, "unsupported_input"),
    # A well-shaped but nonexistent import_root fails in self.reference()'s existence
    # check, which also raises unsupported() -> unsupported_input.
    ({"module": "ok", "attribute": "build", "import_root": "missing-dir"}, "unsupported_input"),
])
def test_launch_v2_rejects_bad_agent(tmp_path, monkeypatch, agent, expected_code):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    launch_path = request["source"].launch_path
    launch_v1 = json.loads(launch_path.read_text())
    document = {key: value for key, value in launch_v1.items() if key != "command"}
    document["schema"] = "lab-target-launch/2"
    document["agent"] = agent
    launch_path.write_text(json.dumps(document))

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)

    # Every expected code is unsupported_input, never unsupported_schema: on the
    # pre-Task-5 code every one of these cases raised unsupported_schema (the /2
    # schema itself was unrecognized), so pinning unsupported_input here fails on
    # that old code and only passes once schema dispatch + agent validation exist.
    assert caught.value.code == expected_code
    assert caught.value.stage == "bundle"


def test_launch_v2_with_command_is_rejected(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    launch_path = request["source"].launch_path
    launch_v1 = json.loads(launch_path.read_text())
    document = dict(launch_v1)
    document["schema"] = "lab-target-launch/2"
    launch_path.write_text(json.dumps(document))

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)

    assert caught.value.code == "invalid_request"
    assert caught.value.stage == "bundle"


def _target_environment_payload(target_lab):
    """Distinct target environment (§6.7); evo-skills always pinned, evolution-lab only when given."""
    distributions = [{"name": "evo-skills", "version": "0.3.0a5", "wheel_sha256": "d" * 64}]
    if target_lab is not None:
        distributions.append({"name": "evolution-lab", "version": target_lab, "wheel_sha256": "e" * 64})
    return {
        "schema": "lab-environment/1", "executable": "/opt/neutral-target/python",
        "executable_sha256": "a" * 64, "python_version": "3.12.11",
        "distributions": distributions,
        "runtime_files": [], "platform": {"system": "Linux", "machine": "x86_64"},
        "proof_profile": "wheel-members/1", "limitations": ["synthetic capture boundary"],
    }


def _with_distinct_target_launch_v2(bundle, root, request, tmp_path, *, target_lab):
    """Point a launch /2 at a distinct target environment carrying (or not) an evolution-lab pin."""
    execution_capture = bundle.capture_environment(request["environments"][0], execution=True)
    target_descriptor = EnvironmentDescriptor.from_payload(_target_environment_payload(target_lab))
    target_capture = CapturedEnvironment(target_descriptor, '{"archive_wheels":[]}')

    def capture(value, *, execution):
        if value.executable == Path(sys.executable):
            assert execution is True
            return execution_capture
        assert value.executable == Path("/opt/neutral-target/python")
        assert execution is False
        return target_capture

    request["environments"] = (
        *request["environments"],
        EnvironmentInput(Path(target_descriptor.executable), (tmp_path / "target.whl",)),
    )
    (root / "agent").mkdir()
    (root / "agent" / "echo_agent.py").write_text("def build():\n    pass\n")
    launch_path = request["source"].launch_path
    launch_v1 = json.loads(launch_path.read_text())
    document = launch_v2(launch_v1, import_root="agent")
    document["environment_ref"] = target_descriptor.digest
    launch_path.write_text(json.dumps(document))
    return capture


@pytest.mark.parametrize("target_lab, expected_codes", [
    ("0.0.3.dev4", []),                # same as the execution environment in `fixture`
    ("0.0.3.dev3", ["lab_version_mismatch"]),
    (None, ["lab_version_mismatch"]),  # evolution-lab not declared for the target environment
])
def test_launch_v2_warns_on_lab_version(tmp_path, monkeypatch, target_lab, expected_codes):
    from evolution_lab.preparation.api import PreparationStore

    bundle, root, request = fixture(tmp_path, monkeypatch)
    capture = _with_distinct_target_launch_v2(bundle, root, request, tmp_path, target_lab=target_lab)
    monkeypatch.setattr(bundle, "capture_environment", capture)

    store = PreparationStore(tmp_path / "state")
    summary = store.create_bundle(**request)

    assert summary.schema == "lab-bundle-summary/3"
    assert [warning.code for warning in summary.warnings] == expected_codes
    assert store.get_bundle(summary.digest).warnings == summary.warnings


def test_launch_v1_never_warns(tmp_path, monkeypatch):
    from evolution_lab.preparation.api import PreparationStore

    bundle, root, request = fixture(tmp_path, monkeypatch)
    store = PreparationStore(tmp_path / "state")

    summary = store.create_bundle(**request)

    assert summary.warnings == ()


def test_bundle_warning_never_changes_the_bundle_digest(tmp_path, monkeypatch):
    from evolution_lab.preparation.warnings import bundle_warnings

    bundle, root, request = fixture(tmp_path, monkeypatch)
    capture = _with_distinct_target_launch_v2(bundle, root, request, tmp_path, target_lab="0.0.3.dev3")
    monkeypatch.setattr(bundle, "capture_environment", capture)

    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    plain = stored.summary()
    warned = stored.summary(bundle_warnings(stored))

    assert plain.warnings == ()
    assert [warning.code for warning in warned.warnings] == ["lab_version_mismatch"]
    assert plain.digest == warned.digest == stored.digest


def test_manifest_15_unknown_key_with_newline_is_a_clean_manifest_invalid(tmp_path, monkeypatch):
    """Core quotes the document's key in its diagnosis; control characters become '?' and the code stays Lab's."""
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    manifest = request["source"].manifest_path
    raw = json.loads(manifest.read_text())
    raw["a\nb"] = 1
    manifest.write_text(json.dumps(raw))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert (caught.value.code, caught.value.stage) == ("manifest_invalid", "bundle")
    message = caught.value.failure.message
    assert "a?b" in message and message.isprintable()


@pytest.mark.parametrize("schema", [[], {}, ["evolution-v2-manifest/14"], 14])
def test_non_string_manifest_schema_is_unsupported_schema(tmp_path, monkeypatch, schema):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    storage = PreparationStorage(tmp_path / "state")
    manifest = request["source"].manifest_path
    raw = json.loads(manifest.read_text())
    raw["schema"] = schema
    manifest.write_text(json.dumps(raw))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)
    assert caught.value.code == "unsupported_schema"


@pytest.mark.parametrize("kind", ["evaluation", "probe"])
def test_dev57_bundle_internal_entry_loads_unchanged(tmp_path, monkeypatch, kind):
    """Reproduce dev57's /1 loader acceptance, then read the saved bundle without rewriting it.

    This is a synthetic legacy fixture, not a claim to have run a b9 environment.
    The composer still writes dev57's document format, including schema.
    """
    from evolution.tree import conformance_manifest
    from evolution_lab.preparation import PreparationStore
    from evolution_lab.preparation import bundle as composer
    from evolution_lab.preparation.storage import canonical_json_bytes
    from test_preparation_evaluation import _evaluation_request
    from test_preparation_probe import _probe_request

    if kind == "evaluation":
        request = _evaluation_request(tmp_path, monkeypatch)
        store = PreparationStore(tmp_path / "state")
    else:
        store, request = _probe_request(tmp_path, monkeypatch)

    native_loader = composer.load_manifest_v2

    def dev57_loader(path):
        if path.parent.name == ".lab":
            document = json.loads(path.read_bytes())
            assert document.pop("schema") == "evolution-v2-manifest/1"
            return conformance_manifest(document, base=path.parent)
        return native_loader(path)

    with monkeypatch.context() as legacy:
        legacy.setattr(composer, "load_manifest_v2", dev57_loader)
        # Fixture creation must not depend on the new read path under test.
        legacy.setattr(composer, "load_internal_manifest", dev57_loader, raising=False)
        summary = store.create_bundle(**request)
    stored = store._storage.get_bundle(summary.digest)
    path = stored.path / stored.descriptor.conformance_entrypoint
    before = {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob("*") if p.is_file()}
    raw = json.loads(before[path.relative_to(stored.path)])
    assert set(raw) == {"schema", "seed_root", "target", "artifacts"}
    assert raw["schema"] == "evolution-v2-manifest/1"
    assert before[path.relative_to(stored.path)] == canonical_json_bytes(raw)
    digest = hashlib.sha256(before[Path("descriptor.json")]).hexdigest()
    assert digest == summary.digest

    from evolution_lab.preparation.internal_manifest import load_internal_manifest

    manifest = load_internal_manifest(path)
    assert manifest.schema == "evolution-v2-manifest/1" and manifest.workflow is None
    assert manifest.seed_root == (path.parent / raw["seed_root"]).resolve()
    if kind == "evaluation":
        prepared = store.prepare_evaluation(summary.digest, selection={"all": True})
        assert prepared.bundle_digest == digest
    assert store.get_bundle(digest).digest == digest
    assert {p.relative_to(stored.path): p.read_bytes() for p in stored.path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("kind", ["evaluation", "probe"])
def test_internal_entry_rejects_extra_keys(tmp_path, monkeypatch, kind):
    from evolution_lab.preparation import PreparationStore
    from evolution_lab.preparation import bundle as composer
    from test_preparation_evaluation import _evaluation_request
    from test_preparation_probe import _probe_request

    if kind == "evaluation":
        request = _evaluation_request(tmp_path, monkeypatch)
        store = PreparationStore(tmp_path / "state")
        original = composer.conformance_payload
        monkeypatch.setattr(composer, "conformance_payload", lambda *a, **kw: {**original(*a, **kw), "extra": True})
    else:
        store, request = _probe_request(tmp_path, monkeypatch)
        original = composer._probe_manifest
        monkeypatch.setattr(composer, "_probe_manifest", lambda *a, **kw: {**original(*a, **kw), "extra": True})
    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**request)
    assert (caught.value.code, caught.value.stage) == ("manifest_invalid", "bundle")
    assert not list((store._storage.state_directory / "bundles").iterdir())


@pytest.mark.parametrize("content", [b"[]", b"null", b"{broken", b'{"target":NaN}'])
def test_internal_entry_invalid_json_is_manifest_error(tmp_path, content):
    from evolution.tree import ManifestError
    from evolution_lab.preparation.internal_manifest import load_internal_manifest

    path = tmp_path / "conformance.json"
    path.write_bytes(content)
    with pytest.raises(ManifestError) as caught:
        load_internal_manifest(path)
    assert (caught.value.code, caught.value.detail) == ("bad_type", "$")


MARKER = 'EVOLUTION_LAB_SCOPE'


def _marker_declaration(tmp_path, monkeypatch, declaration):
    from test_owner_binding import MODEL_GROUPING
    module, root, request = fixture(tmp_path, monkeypatch)
    path = request['source'].manifest_path
    raw = json.loads(path.read_text())
    launch_path = request['source'].launch_path
    launch = json.loads(launch_path.read_text())
    if declaration == 'scorer':
        raw['scorer']['allowed_env'] = [MARKER]
    elif declaration == 'agent_editor':
        raw['editor'] = {'kind': 'agent_cli', 'model': 'codex/gpt-5', 'permission': 'safe',
            'agent_bin': sys.executable, 'allowed_env': [MARKER]}
    elif declaration == 'chat_reflector':
        previous = raw['reflector']
        raw['reflector'] = {key: previous[key] for key in (
            'selection', 'families', 'max_consecutive_failures', 'environment_docs', 'probe') if key in previous}
        raw['reflector'].update(kind='chat', model='neutral', base_url='http://127.0.0.1:9/v1',
            api_key_env=MARKER, context_compact_chars=128000, context_message_max_chars=12000,
            retry=MODEL_GROUPING['retry'])
    elif declaration == 'command_grouper':
        raw['grouper'] = {'kind': 'command', 'argv': [sys.executable, 'reflect.py'],
            'cwd': 'reflector', 'dependency_paths': [], 'allowed_env': [MARKER],
            'timeout_seconds': 7, 'config': {}}
    elif declaration in ('model_key', 'model_proxy'):
        raw['grouper'] = dict(MODEL_GROUPING)
        raw['grouper']['api_key_env' if declaration == 'model_key' else 'proxy_env'] = MARKER
    elif declaration == 'target_secret':
        launch['secret_names'].append(MARKER)
        request['secret_names'] = (*request['secret_names'], MARKER)
    elif declaration == 'target_static':
        launch['env'][MARKER] = 'operator-value'
        request['env'][MARKER] = 'operator-value'
    else:
        raise AssertionError(declaration)
    path.write_text(json.dumps(raw))
    launch_path.write_text(json.dumps(launch))
    load_manifest_v2(path)  # Every refusal case is independently a valid Core manifest.
    return module, root, request


@pytest.mark.parametrize('declaration', ['scorer', 'agent_editor', 'chat_reflector', 'command_grouper',
    'model_key', 'model_proxy', 'target_secret', 'target_static'])
def test_reserved_marker_name_refused_in_every_declaration(tmp_path, monkeypatch, declaration):
    from evolution_lab.owner import protocol
    module, root, request = _marker_declaration(tmp_path, monkeypatch, declaration)
    frames = []
    monkeypatch.setattr(protocol, 'make_config', lambda *a, **kw: frames.append((a, kw)))
    storage = PreparationStorage(tmp_path / 'state')
    with pytest.raises(PreparationError) as failure:
        module.create_bundle(storage, **request)
    assert failure.value.code == 'reserved_environment_name'
    assert failure.value.stage == 'bundle'
    assert frames == []
    assert not list((storage.state_directory / 'bundles').iterdir())


def test_agent_forwarded_names_are_not_declarations(tmp_path, monkeypatch):
    from evolution_lab.owner import secrets
    module, root, request = fixture(tmp_path, monkeypatch)
    manifest_path = request['source'].manifest_path
    raw = json.loads(manifest_path.read_text())
    raw['editor'] = {'kind': 'agent_cli', 'model': 'codex/gpt-5', 'permission': 'unsafe',
        'agent_bin': sys.executable, 'allowed_env': []}
    manifest_path.write_text(json.dumps(raw))
    request['env'][MARKER] = 'outer-value'
    manifest = load_manifest_v2(manifest_path)
    assert MARKER in secrets._agent_forwarded_names(manifest.workflow.editor, set(request['env']))
    names = secrets.declared_environment_names(workflow=manifest.workflow,
        launch=json.loads(request['source'].launch_path.read_text()))
    assert MARKER not in names.execution | names.target_secrets | names.target_static
    assert module.create_bundle(PreparationStorage(tmp_path / 'state'), **request).path.is_dir()


def test_reserved_code_registered():
    from evolution_lab.preparation import models as preparation
    from evolution_lab.owner import models as owner, binding
    from evolution_lab.service import errors
    from evolution_lab.quickstart import _evaluate
    code = 'reserved_environment_name'
    message = 'The environment name EVOLUTION_LAB_SCOPE is reserved by Lab.'
    assert preparation._ERROR_MESSAGES.get(code) == message
    assert owner._ERROR_MESSAGES.get(code) == message
    assert binding._from_preparation(preparation.PreparationError(code, 'bundle'), 'prepare').code == code
    assert errors._STATUS.get(code) == 422
    assert code in _evaluate._PROJECT_CODES
