"""Grouper kinds Lab supports on /15 (spec 2026-10-13 §5.1): fields, command, chat; agent_cli is refused."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from evolution.tree import load_manifest_v2

from evolution_lab._manifest_schema import LAB_GROUPER_KINDS, grouping_section, unsupported_grouper_kind
from scaffold_15 import scaffold_15

_RETRY = {"version": "role_call_retry/1", "max_attempts": 1, "total_budget_seconds": 60,
          "backoff": {"initial_seconds": 0, "multiplier": 1, "max_seconds": 0, "jitter": "full"}}
_AGENT_CLI = {"kind": "agent_cli", "model": "codex", "timeout_seconds": 60}


def _apply(manifest: Path, grouper_of) -> Path:
    """Replace ``grouper`` of the /15 manifest at ``manifest`` by ``grouper_of(manifest_dir)`` (``None`` keeps it)."""
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if grouper_of is not None:
        document["grouper"] = grouper_of(manifest.parent)
    manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return manifest


def _manifest(tmp_path: Path, grouper_of) -> Path:
    return _apply(scaffold_15(tmp_path / "s").manifest, grouper_of)


def _command(manifest_dir: Path) -> dict:
    tool = manifest_dir / "grouper"
    tool.mkdir()
    (tool / "group.py").write_text("print('{}')\n", encoding="utf-8")
    return {"kind": "command", "argv": [sys.executable, "group.py"], "cwd": "grouper",
            "dependency_paths": [], "allowed_env": ["GROUPER_TOKEN"], "timeout_seconds": 30, "config": {}}


def _chat(manifest_dir: Path) -> dict:
    (manifest_dir / "grouper-prompt.md").write_text("Group the failures.\n", encoding="utf-8")
    return {"kind": "chat", "backend": "openai_chat", "model": "fake-model", "base_url": "http://127.0.0.1:9/v1",
            "api_key_env": "GROUPER_KEY", "proxy_env": "GROUPER_PROXY", "timeout_seconds": 30, "concurrency": 1,
            "max_input_chars": 10000, "max_output_tokens": 512, "retry": _RETRY,
            "system_prompt": "grouper-prompt.md"}


def _agent_cli(manifest_dir: Path) -> dict:
    return dict(_AGENT_CLI)


def test_lab_grouper_kinds():
    assert LAB_GROUPER_KINDS == ("fields", "command", "chat")
    assert not unsupported_grouper_kind({"schema": "evolution-v2-manifest/15", "grouper": {"kind": "fields"}})
    assert unsupported_grouper_kind({"schema": "evolution-v2-manifest/15", "grouper": {"kind": "agent_cli"}})
    assert not unsupported_grouper_kind({"schema": "evolution-v2-manifest/14", "reflector": {}})


def test_grouping_section_of_15_is_the_top_level_grouper(tmp_path):
    workflow = load_manifest_v2(_manifest(tmp_path, None)).workflow
    assert grouping_section(workflow) is workflow.grouper
    assert workflow.reflector.grouping is None


@pytest.mark.parametrize("grouper_of, names", [(_command, ("GROUPER_TOKEN",)),
                                               (_chat, ("GROUPER_KEY", "GROUPER_PROXY"))])
def test_grouper_secret_names(tmp_path, grouper_of, names):
    from evolution_lab.owner.secrets import _grouping_names
    workflow = load_manifest_v2(_manifest(tmp_path, grouper_of)).workflow
    assert _grouping_names(grouping_section(workflow)) == names


@pytest.mark.parametrize("grouper_of", [_command, _chat])
def test_grouper_timeout_is_in_the_stop_grace(tmp_path, grouper_of):
    from evolution_lab.runner import _campaign_stop_grace
    fields = load_manifest_v2(_manifest(tmp_path / "f", None))
    grouped = load_manifest_v2(_manifest(tmp_path / "g", grouper_of))
    assert _campaign_stop_grace(grouped) == pytest.approx(_campaign_stop_grace(fields) + 30.0)


def test_command_grouper_is_relocated_and_its_program_is_pinned(tmp_path, monkeypatch):
    from test_preparation_bundle import fixture
    from evolution_lab.preparation.storage import PreparationStorage

    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    _apply(request["source"].manifest_path, _command)
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    saved = json.loads((stored.path / stored.descriptor.entrypoint).read_bytes())["grouper"]
    home = (stored.path / stored.descriptor.entrypoint).parent
    assert (home / saved["cwd"]).resolve().is_relative_to(stored.path.resolve())
    assert (home / saved["cwd"] / saved["argv"][1]).read_text() == "print('{}')\n"


def test_command_grouper_program_is_pinned_by_the_recipe(tmp_path):
    from evolution_lab.recipe import prepare_inputs
    manifest = _manifest(tmp_path, _command)
    prepared = prepare_inputs(manifest, env={"GROUPER_TOKEN": "t" * 12})
    assert manifest.parent / "grouper" / "group.py" in {pin.path for pin in prepared.files}


def test_chat_grouper_system_prompt_is_relocated(tmp_path, monkeypatch):
    from test_preparation_bundle import fixture
    from evolution_lab.preparation.storage import PreparationStorage

    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    _apply(request["source"].manifest_path, _chat)
    stored = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    saved = json.loads((stored.path / stored.descriptor.entrypoint).read_bytes())["grouper"]
    prompt = (stored.path / stored.descriptor.entrypoint).parent / saved["system_prompt"]
    assert prompt.resolve().is_relative_to(stored.path.resolve())
    assert prompt.read_text() == "Group the failures.\n"


def test_chat_grouper_system_prompt_is_pinned_by_the_recipe(tmp_path):
    from evolution_lab.recipe import prepare_inputs
    manifest = _manifest(tmp_path, _chat)
    environment = {"GROUPER_KEY": "k" * 12, "GROUPER_PROXY": "http://127.0.0.1:9/"}
    prepared = prepare_inputs(manifest, env=environment)
    assert manifest.parent / "grouper-prompt.md" in {pin.path for pin in prepared.files}


def test_chat_grouper_values_reach_the_worker_redaction_set(tmp_path):
    from evolution.tree import evolve as evolve_module
    from evolution_lab.owner._campaign_worker import _worker_redactions, _worker_short_forwarded_names

    admitted = evolve_module.admit_manifest(_manifest(tmp_path, _chat))
    environment = {"GROUPER_KEY": "k" * 12, "GROUPER_PROXY": "p"}
    assert _worker_redactions(admitted, environment, ("core",)) == ("core", "k" * 12)
    assert _worker_short_forwarded_names(admitted, environment, ()) == ("GROUPER_PROXY",)


def test_agent_cli_fixture_is_accepted_by_core(tmp_path):
    """The refusal is Lab's: core's loader takes the agent_cli Grouper."""
    manifest = load_manifest_v2(_manifest(tmp_path, _agent_cli))
    assert type(manifest.workflow.grouper).__name__ == "AgentCliGrouperSection"


def test_agent_cli_grouper_is_refused_by_the_form(tmp_path):
    from evolution_lab.projects.package import read_package_entry, validate_form
    sample = scaffold_15(tmp_path / "lab")
    _apply(sample.manifest, _agent_cli)
    (tmp_path / "lab" / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = tmp_path / "lab" / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": sample.manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    assert {"code": "grouper_kind_unsupported",
            "detail": "grouper.kind agent_cli: Lab supports fields, command, chat"} in \
        validate_form(read_package_entry(entry), place=tmp_path / "lab")


def test_grouper_kind_unsupported_message_and_status():
    from evolution_lab.preparation.models import PreparationError
    from evolution_lab.service.errors import _STATUS
    assert PreparationError("grouper_kind_unsupported", "bundle").failure.message == (
        "Lab supports grouper kinds fields, command, chat (grouper.kind).")
    assert _STATUS["grouper_kind_unsupported"] == 422
