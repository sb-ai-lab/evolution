"""The Grouper's bindings (family-14 design R19, A§3.3, A§3.4): credentials,
the secret set's values, the program paths, the command binding and the
agent program seal."""

from __future__ import annotations

import hashlib

import pytest

from evolution.tree import bindings
from evolution.tree.bindings import (
    BindingError,
    build_grouper_command,
    build_grouping_command,
    grouper_agent_seal,
    grouper_environment_names,
    grouper_environment_paths,
    grouper_environment_values,
    grouper_model_credentials,
    grouper_program_paths,
)
from evolution.tree.execution.authorizations import HARD_CODES
from evolution.tree.execution.output_admission import EvolveRefused
from tests.tree.manifest.test_manifest_v15 import (
    AGENT_GROUPER,
    CHAT_GROUPER,
    COMMAND_GROUPER,
    FIELDS_GROUPER,
    load,
    v15_document,
)

KEY = "sk-grouper-0123456789abcdef"


def section(tmp_path, grouper):
    manifest = load(tmp_path, v15_document(grouper=grouper))
    assert manifest.workflow is not None and manifest.workflow.grouper is not None
    return manifest.workflow.grouper


def refusal(call):
    with pytest.raises((EvolveRefused, BindingError)) as caught:
        call()
    return caught.value.code, caught.value.path


def agent(tmp_path, **keys):
    (tmp_path / "bin").mkdir(exist_ok=True)
    program = tmp_path / "bin" / "codex"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    return section(tmp_path, {**AGENT_GROUPER, "agent_bin": "bin/codex", **keys}), program


def test_the_three_binding_codes_are_hard():
    assert {"grouper_invalid", "grouper_env_missing", "grouper_program_missing"} <= HARD_CODES


# -- chat: the grouping model's credentials under grouper.* -----------------------------


def test_a_chat_grouper_reads_its_key_and_proxy(tmp_path):
    chat = section(tmp_path, {**CHAT_GROUPER, "proxy_env": "GROUPER_PROXY"})
    environ = {"GROUPER_API_KEY": KEY, "GROUPER_PROXY": "https://proxy.example:8443"}
    credentials = grouper_model_credentials(chat, environ)
    assert credentials is not None
    assert (credentials.api_key, credentials.proxy_url) == (KEY, "https://proxy.example:8443")
    assert grouper_environment_values(chat, environ) == (KEY, "https://proxy.example:8443")
    assert grouper_environment_names(chat) == frozenset({"GROUPER_API_KEY", "GROUPER_PROXY"})


def test_a_chat_grouper_refuses_under_its_own_vocabulary(tmp_path):
    chat = section(tmp_path, {**CHAT_GROUPER, "proxy_env": "GROUPER_PROXY"})
    assert refusal(lambda: grouper_model_credentials(chat, {})) == (
        "grouper_env_missing",
        "grouper.api_key_env",
    )
    environ = {"GROUPER_API_KEY": KEY, "GROUPER_PROXY": "http://remote.example:8080"}
    assert refusal(lambda: grouper_model_credentials(chat, environ)) == (
        "grouper_invalid",
        "grouper.proxy_env",
    )


def test_a_chat_grouper_without_the_tree_extra_is_refused_at_bind(tmp_path, monkeypatch):
    chat = section(tmp_path, CHAT_GROUPER)
    monkeypatch.setattr(bindings, "_provider_sdks_installed", lambda: False)
    assert refusal(lambda: grouper_model_credentials(chat, {"GROUPER_API_KEY": KEY})) == (
        "grouper_invalid",
        "grouper.backend",
    )


@pytest.mark.parametrize("value", [None, ""])
def test_a_chat_grouper_requires_its_declared_proxy_value(tmp_path, value):
    chat = section(tmp_path, {**CHAT_GROUPER, "proxy_env": "GROUPER_PROXY"})
    environ = {"GROUPER_API_KEY": KEY}
    if value is not None:
        environ["GROUPER_PROXY"] = value
    assert refusal(lambda: grouper_environment_values(chat, environ)) == (
        "grouper_env_missing",
        "grouper.proxy_env",
    )


@pytest.mark.parametrize("proxy", [False, True])
def test_a_chat_grouper_without_api_key_env_is_unauthenticated(tmp_path, proxy):
    """#290 §4.1, C6: an undeclared key is no refusal and no secret value; the
    declared proxy still is one, and the paths stay index for index (#287)."""

    keys = {"proxy_env": "GROUPER_PROXY"} if proxy else {}
    grouper = {key: value for key, value in CHAT_GROUPER.items() if key != "api_key_env"}
    chat = section(tmp_path, {**grouper, **keys})
    environ = {"GROUPER_API_KEY": KEY, "GROUPER_PROXY": "https://proxy.example:8443"}
    credentials = grouper_model_credentials(chat, environ)
    assert credentials is not None
    proxy_url = "https://proxy.example:8443" if proxy else None
    assert (credentials.api_key, credentials.proxy_url) == (None, proxy_url)
    expected = ("https://proxy.example:8443",) if proxy else ()
    assert grouper_environment_values(chat, environ) == expected
    assert grouper_environment_paths(chat, environ) == (("grouper.proxy_env",) if proxy else ())
    assert grouper_environment_names(chat) == (
        frozenset({"GROUPER_PROXY"}) if proxy else frozenset()
    )


@pytest.mark.parametrize("environ", [{}, {"GROUPER_API_KEY": ""}], ids=["absent", "empty"])
def test_a_declared_chat_grouper_key_still_needs_a_value(tmp_path, environ):
    chat = section(tmp_path, CHAT_GROUPER)
    for call in (
        lambda: grouper_model_credentials(chat, environ),
        lambda: grouper_environment_values(chat, environ),
    ):
        assert refusal(call) == ("grouper_env_missing", "grouper.api_key_env")


# -- command: forwarded values join the secret set (R19) ----------------------------------


def test_a_command_grouper_forwards_and_redacts_its_allowed_env(tmp_path):
    command = section(tmp_path, COMMAND_GROUPER)
    assert grouper_environment_names(command) == frozenset({"GROUPER_TOKEN"})
    assert grouper_environment_values(command, {"GROUPER_TOKEN": KEY}) == (KEY,)
    assert grouper_environment_values(command, {}) == ()
    assert grouper_program_paths(command, {}) == ((tmp_path / "grouping").resolve(),)


def test_a_command_grouper_binds_like_a_grouping_command(tmp_path):
    command = section(tmp_path, {**COMMAND_GROUPER, "dependency_paths": []})
    (tmp_path / "grouping").mkdir()
    (tmp_path / "grouping" / "group.py").write_text("print('{}')\n")
    environ = {"PATH": "/usr/bin:/bin"}
    bound = build_grouper_command(
        command.command, seed_root=tmp_path / "seed", run_dir=tmp_path / "run", environ=environ
    )
    same = build_grouping_command(
        command.command, seed_root=tmp_path / "seed", run_dir=tmp_path / "run", environ=environ
    )
    assert bound.identity_digest(timeout_seconds=30.0) == same.identity_digest(timeout_seconds=30.0)


def test_a_command_grouper_binding_refuses_under_its_own_vocabulary(tmp_path):
    command = section(tmp_path, COMMAND_GROUPER)
    assert refusal(
        lambda: build_grouper_command(
            command.command, seed_root=tmp_path / "seed", run_dir=tmp_path / "run", environ={}
        )
    ) == ("grouper_invalid", "grouper.cwd")


@pytest.mark.parametrize(
    "defect,field",
    [
        ("missing_cwd", "cwd"),
        ("missing_executable", "argv"),
        ("directory_executable", "argv"),
        ("missing_dependency", "dependency_paths"),
        ("directory_dependency", "dependency_paths"),
    ],
)
def test_command_grouper_binding_details_never_echo_declared_paths(tmp_path, defect, field):
    from evolution.tree.execution.config import CommandSection

    marker = f"private-command-{defect}-marker"
    program = tmp_path / marker
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    cwd = tmp_path
    dependencies = ()
    if defect == "missing_cwd":
        cwd = tmp_path / f"{marker}-gone"
    elif defect == "missing_executable":
        program.unlink()  # the declared program disappeared before binding
    elif defect == "directory_executable":
        program.unlink()
        program.mkdir()
    elif defect == "missing_dependency":
        dependencies = (tmp_path / f"{marker}-dependency-gone",)
    else:
        dependencies = (tmp_path,)
    command = CommandSection(
        argv=(str(program),),
        cwd=cwd,
        dependency_paths=dependencies,
        allowed_env=("GROUPER_TOKEN",),
        timeout_seconds=30.0,
    )
    with pytest.raises(BindingError) as caught:
        build_grouper_command(
            command,
            seed_root=tmp_path / "seed",
            run_dir=tmp_path / "run",
            environ={"GROUPER_TOKEN": KEY},
        )
    error = caught.value
    assert (error.code, error.path) == ("grouper_invalid", f"grouper.{field}")
    assert error.detail == "command could not be bound"
    for private in (marker, str(tmp_path), KEY):
        assert private not in str(error)
        assert private not in error.detail
        assert all(private not in str(argument) for argument in error.args)
        assert all(private not in note for note in getattr(error, "__notes__", ()))


@pytest.mark.parametrize("vanished", ["program", "dependency"])
@pytest.mark.parametrize("role", ["grouper", "reflector.grouping", "scorer", "editor"])
def test_seal_time_disappearance_is_private_only_on_the_new_grouper_route(
    tmp_path, monkeypatch, vanished, role
):
    from evolution.tree.execution import command_binding
    from evolution.tree.execution.config import CommandSection

    program = tmp_path / "private-seal-program-marker"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    dependency = tmp_path / "private-seal-dependency-marker"
    dependency.write_text("declared dependency")
    victim = program if vanished == "program" else dependency
    file_digest = command_binding.file_digest

    def disappear_before_read(path):
        if path == victim:
            path.unlink()
        return file_digest(path)

    monkeypatch.setattr(command_binding, "file_digest", disappear_before_read)
    command = CommandSection(
        argv=(str(program),),
        cwd=tmp_path,
        dependency_paths=(dependency,),
        allowed_env=("GROUPER_TOKEN",),
        timeout_seconds=30.0,
    )
    build = {
        "grouper": build_grouper_command,
        "reflector.grouping": build_grouping_command,
        "scorer": bindings.build_scorer,
        "editor": bindings.build_editor,
    }[role]
    if role != "grouper":
        # Older routes keep the original OS exception, including its filename.
        with pytest.raises(FileNotFoundError) as caught:
            build(command, seed_root=tmp_path / "seed", run_dir=tmp_path / "run", environ={})
        assert caught.value.filename == str(victim)
        return
    with pytest.raises(BindingError) as caught:
        build(
            command,
            seed_root=tmp_path / "seed",
            run_dir=tmp_path / "run",
            environ={"GROUPER_TOKEN": KEY},
        )
    error = caught.value
    assert (error.code, error.path, error.detail) == (
        "grouper_invalid",
        "grouper",
        "command could not be bound",
    )
    for private in (str(tmp_path), program.name, dependency.name, KEY):
        assert private not in str(error)
        assert private not in error.detail
        assert all(private not in str(argument) for argument in error.args)
        assert all(private not in note for note in getattr(error, "__notes__", ()))


@pytest.mark.parametrize("role", ["scorer", "editor", "reflector.grouping"])
def test_older_command_binding_routes_keep_exact_path_bearing_details(tmp_path, role):
    from evolution.tree.execution.config import CommandSection

    command = CommandSection(
        argv=("/bin/sh",),
        cwd=tmp_path / "legacy-private-marker",
        dependency_paths=(),
        allowed_env=(),
        timeout_seconds=30.0,
    )
    build = {
        "scorer": bindings.build_scorer,
        "editor": bindings.build_editor,
        "reflector.grouping": build_grouping_command,
    }[role]
    with pytest.raises(BindingError) as caught:
        build(command, seed_root=tmp_path / "seed", run_dir=tmp_path / "run", environ={})
    assert (caught.value.code, caught.value.path) == (f"{role}_invalid", f"{role}.cwd")
    assert caught.value.detail == str(caught.value.__cause__)
    assert "legacy-private-marker" in caught.value.detail


def test_command_grouper_preserves_program_inside_seed(tmp_path):
    from evolution.tree.execution.config import CommandSection

    seed = tmp_path / "seed"
    seed.mkdir()
    program = seed / "program"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    command = CommandSection(
        argv=(str(program),),
        cwd=tmp_path,
        dependency_paths=(),
        allowed_env=(),
        timeout_seconds=30.0,
    )
    with pytest.raises(BindingError) as caught:
        build_grouper_command(command, seed_root=seed, run_dir=tmp_path / "run", environ={})
    assert (caught.value.code, caught.value.path, caught.value.detail) == (
        "program_inside_seed",
        "grouper",
        "",
    )


# -- agent_cli: the bound child environment and the sealed program -------------------------


def test_an_agent_grouper_values_are_its_bound_child_environment(tmp_path):
    grouper, _program = agent(tmp_path, allowed_env=["AGENT_TOKEN"])
    environ = {"AGENT_TOKEN": KEY, "OPENAI_API_KEY": "sk-openai-0123456789", "PATH": "/usr/bin"}
    values = grouper_environment_values(grouper, environ)
    assert KEY in values and "sk-openai-0123456789" in values
    assert grouper_environment_names(grouper) == frozenset({"AGENT_TOKEN"})
    assert refusal(lambda: grouper_environment_values(grouper, {"PATH": "/usr/bin"})) == (
        "grouper_env_missing",
        "grouper.allowed_env",
    )


def test_an_agent_grouper_seals_its_program(tmp_path):
    grouper, program = agent(tmp_path)
    seal = grouper_agent_seal(grouper, {"PATH": ""})
    assert seal.sha256 == hashlib.sha256(program.read_bytes()).hexdigest()
    paths = grouper_program_paths(grouper, {"PATH": ""})
    assert [path.resolve() for path in paths] == [program.resolve()]


def test_an_agent_grouper_without_a_program_is_refused(tmp_path):
    grouper = section(tmp_path, AGENT_GROUPER)
    assert refusal(lambda: grouper_program_paths(grouper, {"PATH": str(tmp_path / "empty")})) == (
        "grouper_program_missing",
        "grouper.agent_bin",
    )


def test_an_agent_grouper_seal_refuses_a_disappeared_program(tmp_path):
    program_root = tmp_path / "declared-program-marker"
    program_root.mkdir()
    grouper, program = agent(program_root, allowed_env=["AGENT_TOKEN"])
    program.unlink()
    with pytest.raises(BindingError) as caught:
        grouper_agent_seal(grouper, {"PATH": "", "AGENT_TOKEN": KEY})
    assert (caught.value.code, caught.value.path, caught.value.detail) == (
        "grouper_invalid",
        "grouper.agent_bin",
        "program could not be sealed",
    )
    for marker in (str(program), "declared-program-marker", KEY):
        assert marker not in caught.value.detail
        assert marker not in str(caught.value)
        assert all(marker not in str(argument) for argument in caught.value.args)


def test_the_grouper_program_paths_include_declared_prompts(tmp_path):
    prompt = tmp_path / "grouper-prompt.md"
    prompt.write_text("Group the observations.\n")
    chat = section(tmp_path, {**CHAT_GROUPER, "system_prompt": prompt.name})
    assert grouper_program_paths(chat, {}) == (prompt.resolve(),)
    grouper, program = agent(tmp_path, system_prompt=prompt.name)
    assert grouper_program_paths(grouper, {}) == (prompt.resolve(), program.resolve())


def test_a_grouper_prompt_seal_refusal_has_no_declared_path(tmp_path):
    prompt = tmp_path / "declared-prompt-marker.md"
    prompt.write_text("Group observations.\n")
    prompt.unlink()
    with pytest.raises(BindingError) as caught:
        bindings._sealed_prompt(
            prompt, "unused", code="grouper_invalid", field_path="grouper.system_prompt"
        )
    assert (caught.value.code, caught.value.path, caught.value.detail) == (
        "grouper_invalid",
        "grouper.system_prompt",
        "prompt could not be sealed",
    )
    for marker in (str(prompt), prompt.name):
        assert marker not in caught.value.detail
        assert marker not in str(caught.value)
        assert all(marker not in str(argument) for argument in caught.value.args)


@pytest.mark.parametrize("role", ["editor", "reflector"])
def test_older_role_prompt_seal_refusal_details_stay_unchanged(tmp_path, role):
    prompt = tmp_path / "older-role-prompt-marker.md"
    with pytest.raises(BindingError) as caught:
        bindings._sealed_prompt(
            prompt, "unused", code=f"{role}_invalid", field_path=f"{role}.system_prompt"
        )
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.detail == str(caught.value.__cause__)


def test_a_fields_grouper_has_no_environment_and_no_program(tmp_path):
    fields = section(tmp_path, FIELDS_GROUPER)
    assert grouper_environment_names(fields) == frozenset()
    assert grouper_environment_values(fields, {"X": KEY}) == ()
    assert grouper_program_paths(fields, {}) == ()
    assert grouper_model_credentials(fields, {}) is None
