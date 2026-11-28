"""F-11 приёмки dev48: корпус внутри рабочего каталога роли отказывается на подготовке, а не на Start.

Lab вызывает helper exact b5 до действий на всех путях подготовки. Сверка с `evolve` сохраняет
прежние раскладки отказов, включая документы рефлектора и промпты группировки.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

import pytest
from evolution.tree import load_manifest_v2
from scaffold_15 import scaffold_15

from evolution_lab._manifest_schema import (
    CORPUS_INSIDE_ROLE_WORKSPACE, CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE, corpus_inside_role_workspace)

#: Раскладки, которые ядро отказывает: корпус в seed_root или в cwd одной из ролей.
REFUSED = ("seed", "scorer", "editor", "reflector")


_RETRY = {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 20,
          "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 4, "jitter": "full"}}

#: Раскладки, выраженные через `grouper` и `environment_docs` леса /15; `*_file` кладут корпус поверх файла роли.
GROUPING_REFUSED = ("grouper_command_cwd", "grouper_chat_prompt", "reflector_environment_docs")


def _grouping_layout(sample, raw, where):
    """Меняет сырой манифест /15 под раскладку `where`; корпус кладётся внутрь каталога роли или поверх её файла."""
    base = sample.manifest.parent
    if where == "grouper_command_cwd":
        (base / "grouper").mkdir()
        (base / "grouper" / "group.py").write_text("print('{}')\n", encoding="utf-8")
        raw["grouper"] = {"kind": "command", "argv": ["python3", "group.py"], "cwd": "grouper",
                          "dependency_paths": [], "allowed_env": [], "timeout_seconds": 30, "config": {}}
        target = "grouper/corpus.jsonl"
        shutil.move(base / raw["corpus"]["path"], base / target)
    elif where == "grouper_chat_prompt":
        raw["grouper"] = {"kind": "chat", "backend": "openai_chat", "model": "fake-model",
                          "base_url": "http://127.0.0.1:9/v1", "api_key_env": "GROUPER_KEY", "timeout_seconds": 30,
                          "concurrency": 1, "max_input_chars": 10000, "max_output_tokens": 512, "retry": _RETRY,
                          "system_prompt": "corpus.jsonl"}
        target = "corpus.jsonl"
    else:
        (base / "docs").mkdir()
        raw["reflector"]["environment_docs"] = "docs"
        target = "docs/corpus.jsonl"
        shutil.move(base / raw["corpus"]["path"], base / target)
    raw["corpus"]["path"] = target


#: Раскладки с ролью `agent_cli`, чей CLI объявлен (`agent_bin`), а не ищется на PATH: корпус - это объявленный промпт
#: или сам `agent_bin`. Проверка Lab смотрит только на объявленные пути (codex r1 на dev48, находка 2).
AGENT_CLI_REFUSED = ("editor_agent_prompt", "editor_agent_bin", "reflector_agent_prompt", "grouper_agent_prompt")
_REFLECTOR_COMMON = ("environment_docs", "probe", "max_consecutive_failures", "selection", "families")


def _agent_cli_layout(sample, raw, where):
    corpus = raw["corpus"]["path"]
    agent = {"kind": "agent_cli", "model": "codex", "agent_bin": sys.executable, "system_prompt": corpus}
    if where == "editor_agent_prompt":
        raw["editor"] = agent
    elif where == "editor_agent_bin":
        os.chmod(sample.manifest.parent / corpus, 0o755)
        raw["editor"] = {"kind": "agent_cli", "model": "codex", "agent_bin": corpus}
    elif where == "reflector_agent_prompt":
        raw["reflector"] = {**{key: raw["reflector"][key] for key in _REFLECTOR_COMMON if key in raw["reflector"]},
                            **agent}
    else:
        raw["grouper"] = {**agent, "timeout_seconds": 60}


def _move_corpus(tmp_path, where):
    sample = scaffold_15(tmp_path / "sample")
    raw = json.loads(sample.manifest.read_text())
    if where in AGENT_CLI_REFUSED:
        _agent_cli_layout(sample, raw, where)
        sample.manifest.write_text(json.dumps(raw))
    elif where in GROUPING_REFUSED:
        _grouping_layout(sample, raw, where)
        sample.manifest.write_text(json.dumps(raw))
    elif where is not None:
        directory = raw["seed_root"] if where == "seed" else raw[where]["cwd"]
        shutil.move(sample.manifest.parent / raw["corpus"]["path"], sample.manifest.parent / directory / "corpus.jsonl")
        raw["corpus"]["path"] = f"{directory}/corpus.jsonl"
        sample.manifest.write_text(json.dumps(raw))
    return sample.manifest


@pytest.mark.parametrize("where", REFUSED + GROUPING_REFUSED + AGENT_CLI_REFUSED)
def test_corpus_inside_a_role_workspace_is_detected(tmp_path, where):
    assert corpus_inside_role_workspace(load_manifest_v2(_move_corpus(tmp_path, where)))


def test_corpus_beside_the_roles_is_not(tmp_path):
    assert not corpus_inside_role_workspace(load_manifest_v2(_move_corpus(tmp_path, None)))


def test_families_without_the_core_guard_are_not_checked(tmp_path):
    manifest = load_manifest_v2(_move_corpus(tmp_path, "scorer"))
    assert not corpus_inside_role_workspace(manifest.__class__(**{**manifest.__dict__, "schema": "evolution-v2-manifest/11"}))


@pytest.mark.parametrize("where", REFUSED + GROUPING_REFUSED + AGENT_CLI_REFUSED)
def test_core_evolve_refuses_the_same_layouts(tmp_path, where):
    """Core helper и `evolve` отказывают те же раскладки до записи в run_dir и до сокетов."""
    from evolution.tree.evolve import EvolveRefused, evolve

    manifest = _move_corpus(tmp_path, where)
    run = tmp_path / "run"
    with pytest.raises(EvolveRefused) as caught:
        asyncio.run(evolve(manifest, run, environ={**os.environ, "GROUPER_KEY": "k"}))
    assert caught.value.code == CORPUS_INSIDE_ROLE_WORKSPACE


@pytest.mark.parametrize("where", REFUSED + GROUPING_REFUSED + AGENT_CLI_REFUSED)
def test_core_check_corpus_placement_refuses_the_same_layouts(tmp_path, where):
    """Exact b5 helper отказывает все прежние раскладки Lab, включая документы и grouping prompts.

    Путь импорта вне обещаний беты (PDR-0026 ядра); переезд функции ломает этот тест, а не подготовку.
    """
    from evolution.tree.evolve import EvolveRefused, check_corpus_placement

    manifest = load_manifest_v2(_move_corpus(tmp_path, where))
    with pytest.raises(EvolveRefused) as caught:
        check_corpus_placement(manifest)
    assert caught.value.code == CORPUS_INSIDE_ROLE_WORKSPACE


def test_core_check_corpus_placement_accepts_the_corpus_beside_the_roles(tmp_path):
    from evolution.tree.evolve import check_corpus_placement

    manifest = load_manifest_v2(_move_corpus(tmp_path, None))
    assert check_corpus_placement(manifest) is None


def test_core_check_corpus_placement_on_b5_does_not_resolve_cli_on_path(tmp_path, monkeypatch):
    from evolution.tree.evolve import check_corpus_placement

    manifest = load_manifest_v2(_agent_cli_editor_off_path(tmp_path, monkeypatch))
    assert check_corpus_placement(manifest) is None
    assert corpus_inside_role_workspace(manifest) is False


def test_refusal_message_and_status():
    from evolution_lab.preparation.models import PreparationError
    from evolution_lab.service.errors import _STATUS
    failure = PreparationError(CORPUS_INSIDE_ROLE_WORKSPACE, "bundle").failure
    assert failure.message == CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE
    assert _STATUS[CORPUS_INSIDE_ROLE_WORKSPACE] == 422


def test_form_refuses_it(tmp_path):
    from evolution_lab.projects.package import read_package_entry, validate_form
    manifest = _move_corpus(tmp_path, "scorer")
    place = manifest.parent
    (place / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = place / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    assert {"code": CORPUS_INSIDE_ROLE_WORKSPACE, "detail": CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE} in \
        validate_form(read_package_entry(entry), place=place)


def test_legacy_recipe_refuses_it_before_pinning(tmp_path):
    from evolution_lab.recipe import CorpusInsideRoleWorkspace, prepare_inputs
    manifest = _move_corpus(tmp_path, "scorer")
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(CorpusInsideRoleWorkspace) as caught:
        prepare_inputs(manifest)
    assert caught.value.code == CORPUS_INSIDE_ROLE_WORKSPACE
    assert isinstance(caught.value, ValueError)
    assert sorted(tmp_path.rglob("*")) == before


def test_connect_refuses_it_before_any_effect(tmp_path):
    from evolution_lab.client import ClientError, connect
    manifest = _move_corpus(tmp_path, "scorer")
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(ClientError) as caught:
        connect(manifest)
    assert caught.value.failure.public() == {"code": CORPUS_INSIDE_ROLE_WORKSPACE, "stage": "connect"}
    assert sorted(tmp_path.rglob("*")) == before


def _missing_corpus_beside_a_file_role(tmp_path):
    """Корпуса нет на диске, а путь роли - файл (промпт chat Grouper'а): `samefile` не должен бросать."""
    sample = scaffold_15(tmp_path / "sample")
    raw = json.loads(sample.manifest.read_text())
    _grouping_layout(sample, raw, "grouper_chat_prompt")
    (sample.manifest.parent / "prompt.md").write_text("Group.\n", encoding="utf-8")
    raw["grouper"]["system_prompt"] = "prompt.md"
    (sample.manifest.parent / "corpus.jsonl").unlink()
    sample.manifest.write_text(json.dumps(raw))
    return sample.manifest


def test_missing_corpus_beside_a_file_role_is_left_to_core(tmp_path):
    manifest = load_manifest_v2(_missing_corpus_beside_a_file_role(tmp_path))
    assert corpus_inside_role_workspace(manifest) is False


def test_form_does_not_raise_on_a_missing_corpus(tmp_path):
    from evolution_lab.projects.package import read_package_entry, validate_form
    manifest = _missing_corpus_beside_a_file_role(tmp_path)
    place = manifest.parent
    (place / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = place / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    problems = validate_form(read_package_entry(entry), place=place)
    assert all(problem["code"] != CORPUS_INSIDE_ROLE_WORKSPACE for problem in problems)


def test_connect_does_not_crash_on_a_missing_corpus(tmp_path):
    from evolution_lab.client import ClientError, connect
    manifest = _missing_corpus_beside_a_file_role(tmp_path)
    try:
        connect(manifest)
    except ClientError as error:
        assert error.failure.public()["code"] != CORPUS_INSIDE_ROLE_WORKSPACE
    except FileNotFoundError:  # pragma: no cover
        pytest.fail("the placement check must not raise on a missing corpus")


def _agent_cli_editor_off_path(tmp_path, monkeypatch):
    """/15 с редактором `agent_cli`, чей CLI не найден на PATH; корпус отделён правильно (codex r1, находка 2)."""
    sample = scaffold_15(tmp_path / "sample")
    raw = json.loads(sample.manifest.read_text())
    raw["editor"] = {"kind": "agent_cli", "model": "codex"}
    sample.manifest.write_text(json.dumps(raw))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return sample.manifest


def test_an_agent_cli_off_path_is_not_resolved_by_the_placement_check(tmp_path, monkeypatch):
    """Проверка размещения смотрит только на объявленные пути: отсутствующий CLI - отказ ядра на старте, не здесь."""
    manifest = load_manifest_v2(_agent_cli_editor_off_path(tmp_path, monkeypatch))
    assert corpus_inside_role_workspace(manifest) is False


def test_form_does_not_crash_on_an_agent_cli_off_path(tmp_path, monkeypatch):
    from evolution_lab.projects.package import read_package_entry, validate_form
    manifest = _agent_cli_editor_off_path(tmp_path, monkeypatch)
    place = manifest.parent
    (place / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = place / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    problems = validate_form(read_package_entry(entry), place=place)
    assert all(problem["code"] != CORPUS_INSIDE_ROLE_WORKSPACE for problem in problems)


def test_connect_accepts_an_agent_cli_off_path(tmp_path, monkeypatch):
    """`EvolveRefused` - это `ValueError`: без правки connect отказывал валидный манифест как `manifest_invalid`."""
    from evolution_lab.client import connect
    manifest = _agent_cli_editor_off_path(tmp_path, monkeypatch)
    assert connect(manifest).editor_kind == "agent_cli"


@pytest.mark.parametrize("refused", [False, True])
def test_wrapper_calls_core_with_only_manifest(tmp_path, monkeypatch, refused):
    import importlib
    core = importlib.import_module("evolution.tree.evolve")
    manifest = load_manifest_v2(_move_corpus(tmp_path, None))
    seen = []
    def check(value):
        seen.append(value)
        if refused:
            raise core.EvolveRefused(CORPUS_INSIDE_ROLE_WORKSPACE)
    monkeypatch.setattr(core, "check_corpus_placement", check)
    assert corpus_inside_role_workspace(manifest) is refused
    assert seen == [manifest]


def _helper_error(kind):
    from evolution.tree.evolve import EvolveRefused
    if kind == "refused":
        return EvolveRefused("editor_program_missing", detail="PRIVATE helper detail")
    return {"value": ValueError, "io": OSError}[kind]("PRIVATE helper detail")


@pytest.mark.parametrize("kind", ["refused", "value", "io"])
def test_wrapper_propagates_other_helper_errors(tmp_path, monkeypatch, kind):
    import importlib
    core = importlib.import_module("evolution.tree.evolve")
    manifest = load_manifest_v2(_move_corpus(tmp_path, None))
    error = _helper_error(kind)
    def check(value):
        raise error
    monkeypatch.setattr(core, "check_corpus_placement", check)
    with pytest.raises(type(error)) as caught:
        corpus_inside_role_workspace(manifest)
    assert caught.value is error


def _form_request(manifest):
    from evolution_lab.projects.package import read_package_entry
    place = manifest.parent
    (place / "launch.json").write_text(json.dumps({"schema": "lab-target-launch/2"}))
    entry = place / "bundle-request.json"
    entry.write_text(json.dumps({
        "source": {"kind": "campaign", "manifest_path": manifest.name, "launch_path": "launch.json"},
        "environments": [{"kind": "service"}], "env": {}, "secret_names": [],
        "roots": [{"alias": "lab", "source_path": "."}], "path_bindings": []}))
    return read_package_entry(entry)


@pytest.mark.parametrize("kind", ["refused", "value", "io"])
@pytest.mark.parametrize("entrypoint", ["connect", "recipe", "bundle", "form"])
def test_entrypoints_map_other_helper_errors(tmp_path, monkeypatch, kind, entrypoint):
    import importlib
    from pathlib import Path
    core = importlib.import_module("evolution.tree.evolve")
    manifest = _move_corpus(tmp_path, None)
    request = _form_request(manifest)
    error = _helper_error(kind)
    def check(value):
        raise error
    monkeypatch.setattr(core, "check_corpus_placement", check)
    if entrypoint == "connect":
        from evolution_lab.client import ClientError, connect
        with pytest.raises(ClientError) as caught:
            connect(manifest)
        failure = caught.value.failure.public()
        assert failure == {"code": "io_error" if kind == "io" else "manifest_invalid", "stage": "connect"}
    elif entrypoint == "recipe":
        from evolution_lab.recipe import PreparationArtifactsInvalid, prepare_inputs
        with pytest.raises(PreparationArtifactsInvalid):
            prepare_inputs(manifest)
    elif entrypoint == "bundle":
        from evolution_lab.preparation.bundle import create_bundle
        from evolution_lab.preparation.models import CampaignSource, EnvironmentInput, PreparationError, SourceRoot
        from evolution_lab.preparation.storage import PreparationStorage
        with pytest.raises(PreparationError) as caught:
            create_bundle(PreparationStorage(tmp_path / "state"),
                source=CampaignSource(manifest, manifest.parent / "launch.json"),
                environments=(EnvironmentInput(Path(sys.executable), (tmp_path / "retained.whl",)),),
                roots=(SourceRoot("project", manifest.parent),), path_bindings=(), env={}, secret_names=())
        assert caught.value.code == ("io_error" if kind == "io" else "manifest_invalid")
        assert caught.value.stage == "bundle"
        assert "PRIVATE" not in caught.value.failure.message
    else:
        from evolution_lab.projects.package import validate_form
        problems = validate_form(request, place=manifest.parent)
        expected = "unsupported_input" if kind == "io" else "manifest_invalid"
        assert len(problems) == 1
        assert problems[0]["code"] == expected
        assert "PRIVATE" not in problems[0]["detail"]


@pytest.mark.parametrize("kind", ["refused", "value", "io"])
def test_bundle_source_drift_precedes_helper_error(tmp_path, monkeypatch, kind):
    import importlib
    from pathlib import Path
    from evolution_lab.preparation.bundle import create_bundle
    from evolution_lab.preparation.models import CampaignSource, EnvironmentInput, PreparationError, SourceRoot
    from evolution_lab.preparation.storage import PreparationStorage
    manifest = _move_corpus(tmp_path, None)
    _form_request(manifest)
    def check(value):
        manifest.write_text(manifest.read_text() + " ")
        raise _helper_error(kind)
    monkeypatch.setattr(importlib.import_module("evolution.tree.evolve"), "check_corpus_placement", check)
    with pytest.raises(PreparationError) as caught:
        create_bundle(PreparationStorage(tmp_path / "state"),
            source=CampaignSource(manifest, manifest.parent / "launch.json"),
            environments=(EnvironmentInput(Path(sys.executable), (tmp_path / "retained.whl",)),),
            roots=(SourceRoot("project", manifest.parent),), path_bindings=(), env={}, secret_names=())
    assert caught.value.code == "inputs_changed"
