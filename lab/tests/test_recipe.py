"""Native preparation seals declared inputs without rewriting campaign semantics."""
import json
import os
from pathlib import Path
from types import MappingProxyType

import pytest
from scaffold_15 import scaffold_15


def sample(tmp_path):
    root = tmp_path / "sample"
    scaffold_15(root)
    return root / "manifest.json"


def test_preparation_is_cwd_independent_and_does_not_rewrite_manifest(tmp_path, monkeypatch):
    from evolution_lab.recipe import prepare_inputs
    path = sample(tmp_path); before = path.read_bytes()
    monkeypatch.chdir(tmp_path.parent)
    prepared = prepare_inputs(path)
    prepared.verify()
    assert path.read_bytes() == before
    assert prepared.manifest.path == path
    assert len(prepared.cases) == 2
    assert prepared.public()["seed"]["candidate_digest"]
    assert {p.path for p in prepared.files} >= {path, path.parent / "corpus.jsonl"}


@pytest.mark.parametrize("relative", [
    "manifest.json", "corpus.jsonl", "scorer/score.py", "editor/edit.py",
    "reflector/reflect.py", "seed/skills/SKILL.md", "seed/uv.lock",
])
def test_each_declared_input_drift_is_refused(tmp_path, relative):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs
    path = sample(tmp_path); prepared = prepare_inputs(path)
    target = path.parent / relative
    with target.open("a") as stream: stream.write("\nchanged\n")
    with pytest.raises(InputChanged): prepared.verify()


def test_unsafe_seed_drift_is_refused_as_input_changed(tmp_path):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    path = sample(tmp_path)
    prepared = prepare_inputs(path)
    (path.parent / "seed" / "unsafe-link").symlink_to("uv.lock")

    with pytest.raises(InputChanged):
        prepared.verify()


def test_chat_credentials_are_checked_for_drift_and_never_exported(tmp_path, monkeypatch):
    from evolution_lab.recipe import prepare_inputs
    from evolution_lab.provenance import InputChanged
    path = sample(tmp_path)
    raw = json.loads(path.read_text())
    raw["editor"] = {"kind": "chat", "model": "offline", "base_url": "http://127.0.0.1:9000/editor/v1",
                     "api_key_env": "LAB_CHAT_KEY",
                     "context_compact_chars": 128_000, "context_message_max_chars": 12_000,
                     # /15 requires the author's own retry on a chat role; Lab never supplies it (PDR-0015).
                     "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 60,
                               "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 8,
                                           "jitter": "full"}}}
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("LAB_CHAT_KEY", "CHAT-SECRET-CANARY")
    prepared = prepare_inputs(path)
    assert "CHAT-SECRET-CANARY" not in str(prepared)
    assert "CHAT-SECRET-CANARY" not in json.dumps(prepared.public())
    monkeypatch.setenv("LAB_CHAT_KEY", "rotated")
    with pytest.raises(InputChanged): prepared.verify()


def _model_grouping_sample(tmp_path, *, system_prompt=False):
    from test_owner_binding import MODEL_GROUPING

    path = sample(tmp_path)
    raw = json.loads(path.read_text())
    raw["grouper"] = dict(MODEL_GROUPING)
    if system_prompt:
        (path.parent / "grouping.md").write_text("group the failures by cause\n")
        raw["grouper"]["system_prompt"] = "grouping.md"
    path.write_text(json.dumps(raw))
    environment = MappingProxyType({**os.environ, "GROUPING_KEY": "GROUPING-KEY-VALUE",
                                    "GROUPING_PROXY": "http://127.0.0.1:9/"})
    return path, environment


def test_model_grouping_is_prepared_without_a_grouping_command(tmp_path):
    from evolution_lab.recipe import prepare_inputs

    path, environment = _model_grouping_sample(tmp_path)
    prepared = prepare_inputs(path, env=environment)
    prepared.verify(env=environment)

    # The same manifest with the scaffold's `fields` grouping binds exactly the same commands:
    # a model grouping adds no command (scorer, editor, reflector only).
    baseline_path = sample(tmp_path / "baseline")
    baseline = prepare_inputs(baseline_path)
    def shape(inputs):
        return [(command.argv[1:], command.cwd.relative_to(inputs.manifest.path.parent)) for command in inputs.commands]
    assert shape(prepared) == shape(baseline)
    assert len(prepared.commands) == 3
    assert "GROUPING-KEY-VALUE" not in str(prepared)
    assert "GROUPING-KEY-VALUE" not in json.dumps(prepared.public())


def test_model_grouping_system_prompt_is_pinned_and_its_drift_is_refused(tmp_path):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    path, environment = _model_grouping_sample(tmp_path, system_prompt=True)
    prepared = prepare_inputs(path, env=environment)

    prompt = path.parent / "grouping.md"
    assert prompt in {pin.path for pin in prepared.files}
    prepared.verify(env=environment)
    with prompt.open("a") as stream:
        stream.write("changed\n")
    with pytest.raises(InputChanged):
        prepared.verify(env=environment)


def test_native_schema_is_validated_by_core(tmp_path):
    from evolution.tree import ManifestError
    from evolution_lab.recipe import prepare_inputs
    path = sample(tmp_path)
    raw = json.loads(path.read_text()); raw["hidden_override"] = True
    path.write_text(json.dumps(raw))
    with pytest.raises(ManifestError): prepare_inputs(path)


def test_metadata_contains_no_forwarded_credential_values(tmp_path, monkeypatch):
    from evolution_lab.recipe import prepare_inputs
    path = sample(tmp_path)
    raw = json.loads(path.read_text()); raw["scorer"]["allowed_env"] = ["LAB_TEST_SECRET"]
    path.write_text(json.dumps(raw)); monkeypatch.setenv("LAB_TEST_SECRET", "SECRET-CANARY")
    prepared = prepare_inputs(path)
    assert "SECRET-CANARY" not in json.dumps(prepared.public())
    monkeypatch.setenv("LAB_TEST_SECRET", "rotated")
    from evolution_lab.provenance import InputChanged
    with pytest.raises(InputChanged): prepared.verify()


def test_explicit_environment_is_used_without_reading_or_mutating_process_environment(
    tmp_path, monkeypatch
):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    path = sample(tmp_path)
    raw = json.loads(path.read_text())
    raw["scorer"]["allowed_env"] = ["LAB_SCORER_TOKEN"]
    raw["editor"]["allowed_env"] = ["LAB_EDITOR_TOKEN"]
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("LAB_SCORER_TOKEN", "global-scorer")
    monkeypatch.setenv("LAB_EDITOR_TOKEN", "global-editor")
    before = dict(os.environ)
    explicit = {
        "PATH": os.environ["PATH"],
        "LAB_SCORER_TOKEN": "first-scorer",
        "LAB_EDITOR_TOKEN": "first-editor",
    }

    prepared = prepare_inputs(path, env=explicit)
    prepared.verify()
    prepared.verify(env=explicit)
    assert dict(os.environ) == before
    with pytest.raises(InputChanged):
        prepared.verify(env={**explicit, "LAB_EDITOR_TOKEN": "second-editor"})
    with pytest.raises(InputChanged):
        prepared.verify(env={**explicit, "UNRELATED_REVISION": "two"})
    assert dict(os.environ) == before


def test_explicit_environment_copies_a_read_only_input_mapping(tmp_path):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    path = sample(tmp_path)
    original = {"PATH": os.environ["PATH"], "UNRELATED_REVISION": "first"}
    supplied = MappingProxyType(original)

    prepared = prepare_inputs(path, env=supplied)
    original["UNRELATED_REVISION"] = "second"

    prepared.verify(env={"PATH": os.environ["PATH"], "UNRELATED_REVISION": "first"})
    with pytest.raises(InputChanged):
        prepared.verify(env=supplied)


def test_explicit_preparations_pin_canonical_three_role_bindings_independently(
    tmp_path, monkeypatch
):
    from evolution.tree.execution.command_binding import CommandBinding
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    first_manifest = sample(tmp_path / "first")
    second_manifest = sample(tmp_path / "second")
    second_raw = json.loads(second_manifest.read_text())
    second_raw["campaign"]["rounds"] = 3
    second_manifest.write_text(json.dumps(second_raw))
    monkeypatch.setenv("AMBIENT_ONLY", "must-not-merge")
    first_env = {"PATH": os.environ["PATH"], "REVISION": "first"}
    second_env = {"PATH": os.environ["PATH"], "REVISION": "second"}

    first = prepare_inputs(first_manifest, env=first_env)
    second = prepare_inputs(second_manifest, env=second_env)

    assert len(first.commands) == 3
    assert all(type(command) is CommandBinding for command in first.commands)
    assert [command.cwd.name for command in first.commands] == [
        "scorer", "editor", "reflector"
    ]
    assert all(dict(command.base_env) == first_env for command in first.commands)
    assert all("AMBIENT_ONLY" not in command.base_env for command in first.commands)
    first.verify()
    second.verify()
    with pytest.raises(InputChanged):
        first.verify(env=second_env)
    second_manifest.write_text(second_manifest.read_text() + "\n")
    first.verify()
    with pytest.raises(InputChanged):
        second.verify()


@pytest.mark.parametrize("mutation", ["change", "delete"])
def test_environment_docs_render_digest_is_public_and_detects_drift(tmp_path, mutation):
    from evolution_lab.provenance import InputChanged
    from evolution_lab.recipe import prepare_inputs

    manifest = sample(tmp_path)
    docs = manifest.parent / "environment-docs"
    docs.mkdir()
    guide = docs / "capabilities.md"
    guide.write_text("# Neutral capabilities\n")
    raw = json.loads(manifest.read_text())
    raw["reflector"]["environment_docs"] = "environment-docs"
    manifest.write_text(json.dumps(raw))

    prepared = prepare_inputs(manifest, env={"PATH": os.environ["PATH"]})
    assert len(prepared.public()["environment_docs_digest"]) == 64
    assert docs in prepared.program_paths
    assert docs not in {pin.path for pin in prepared.files}
    if mutation == "change":
        guide.write_text("# Changed capabilities\n")
    else:
        guide.unlink()
    with pytest.raises(InputChanged):
        prepared.verify()


def test_explicit_environment_never_resolves_commands_from_ambient_path(
    tmp_path, monkeypatch
):
    from evolution_lab.recipe import PreparationArtifactsInvalid, prepare_inputs

    manifest = sample(tmp_path)
    ambient_bin = tmp_path / "ambient-bin"
    ambient_bin.mkdir()
    executable = ambient_bin / "ambient-only"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(ambient_bin))
    raw = json.loads(manifest.read_text())
    raw["scorer"]["argv"][0] = "ambient-only"
    manifest.write_text(json.dumps(raw))

    with pytest.raises(PreparationArtifactsInvalid):
        prepare_inputs(manifest, env={})


def test_linked_run_is_refused_before_pinning(tmp_path):
    from evolution_lab.recipe import LinkedRunUnsupported, prepare_inputs
    path = sample(tmp_path)
    raw = json.loads(path.read_text())
    raw["linked_from"] = {"run_dir": "../previous-run", "baseline": "inherit"}
    path.write_text(json.dumps(raw))
    with pytest.raises(LinkedRunUnsupported) as caught:
        prepare_inputs(path)
    assert caught.value.code == "linked_run_unsupported"
    assert isinstance(caught.value, ValueError)


def test_baseline_import_is_refused_before_pinning(tmp_path):
    from evolution_lab.recipe import BaselineImportUnsupported, prepare_inputs

    path = sample(tmp_path)
    raw = json.loads(path.read_text())
    raw["baseline_import"] = {"source": "../evaluation"}
    path.write_text(json.dumps(raw))
    with pytest.raises(BaselineImportUnsupported) as caught:
        prepare_inputs(path)
    assert caught.value.code == "baseline_import_unsupported"


def test_agent_cli_grouper_is_refused_before_pinning(tmp_path):
    """PDR-0015: core accepts a /15 ``agent_cli`` Grouper; Lab's legacy preparation refuses it before any effect."""
    from evolution.tree import load_manifest_v2
    from evolution_lab.recipe import GrouperKindUnsupported, prepare_inputs
    path = scaffold_15(tmp_path / "sample").manifest
    raw = json.loads(path.read_text())
    raw["grouper"] = {"kind": "agent_cli", "model": "codex", "timeout_seconds": 60}
    path.write_text(json.dumps(raw))
    assert type(load_manifest_v2(path).workflow.grouper).__name__ == "AgentCliGrouperSection"
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(GrouperKindUnsupported) as caught:
        prepare_inputs(path)
    assert caught.value.code == "grouper_kind_unsupported"
    assert str(caught.value) == "Lab supports grouper kinds fields, command, chat (grouper.kind)."
    assert isinstance(caught.value, ValueError)
    assert sorted(tmp_path.rglob("*")) == before
