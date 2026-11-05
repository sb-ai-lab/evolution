"""Behavioral contracts for the editor's declared-configuration seals and identity."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from evolution.prompts import TREE_EDITOR_SYSTEM
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.config import AgentCliEditorSection, ChatEditorSection
from evolution.tree.roles.editor.editor_brief import BRIEF_VERSION
from evolution.tree.roles.seals import (
    CHAT_TOOL_SCHEMA,
    FileSeal,
    SealChanged,
    agent_identity,
    agent_reflector_identity,
    chat_identity,
    chat_reflector_identity,
    seal_program,
    seal_prompt,
    section_payload,
)
from evolution.tree.roles.workspace import Budgets

BUDGETS = Budgets(max_changed_files=8, max_written_bytes=1_000_000, max_payload_bytes=200_000)


def make_chat_section(**overrides: object) -> ChatEditorSection:
    fields = {
        "model": "gpt-x",
        "base_url": "https://api.example/v1",
        "api_key_env": "EDITOR_API_KEY",
        "budgets": BUDGETS,
    }
    fields.update(overrides)
    return ChatEditorSection(**fields)  # type: ignore[arg-type]


def make_agent_section(**overrides: object) -> AgentCliEditorSection:
    fields = {
        "model": "claude-x",
        "budgets": BUDGETS,
    }
    fields.update(overrides)
    return AgentCliEditorSection(**fields)  # type: ignore[arg-type]


def write_prompt(path: Path, text: str = "you are an editor\n") -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def write_fake_cli(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_chat_digest_is_stable_across_values_and_paths(tmp_path: Path) -> None:
    prompt_a = write_prompt(tmp_path / "prompt_a.md")
    (tmp_path / "nested").mkdir()
    prompt_b = write_prompt(tmp_path / "nested" / "prompt_b.md")
    section = make_chat_section()

    seal_a = seal_prompt(prompt_a, packaged=TREE_EDITOR_SYSTEM)
    seal_b = seal_prompt(prompt_b, packaged=TREE_EDITOR_SYSTEM)
    digest_a = chat_identity(section, seal_a).digest
    digest_b = chat_identity(section, seal_b).digest

    assert digest_a == digest_b
    # Two independently computed digests over the same declared inputs agree.
    assert (
        digest_a
        == chat_identity(section, seal_prompt(prompt_a, packaged=TREE_EDITOR_SYSTEM)).digest
    )

    write_prompt(prompt_b, "you are a different editor\n")
    seal_b_changed = seal_prompt(prompt_b, packaged=TREE_EDITOR_SYSTEM)
    digest_b_changed = chat_identity(section, seal_b_changed).digest
    assert digest_b_changed != digest_a

    section_other_env = make_chat_section(api_key_env="OTHER_KEY_ENV")
    digest_other_env = chat_identity(section_other_env, seal_a).digest
    assert digest_other_env != digest_a


def test_agent_digest_tracks_program_bytes_not_path(tmp_path: Path) -> None:
    cli_a_dir = tmp_path / "bin_a"
    cli_a_dir.mkdir(parents=True, exist_ok=True)
    cli_a = write_fake_cli(cli_a_dir / "agent-cli")
    cli_b_dir = tmp_path / "bin_b"
    cli_b_dir.mkdir(parents=True, exist_ok=True)
    cli_b = write_fake_cli(cli_b_dir / "agent-cli")

    section = make_agent_section()
    prompt_seal = seal_prompt(None, packaged=TREE_EDITOR_SYSTEM)

    digest_a = agent_identity(section, prompt_seal, seal_program(cli_a)).digest
    digest_b = agent_identity(section, prompt_seal, seal_program(cli_b)).digest
    assert digest_a == digest_b

    write_fake_cli(cli_b, "#!/bin/sh\nexit 1\n")
    digest_b_changed = agent_identity(section, prompt_seal, seal_program(cli_b)).digest
    assert digest_b_changed != digest_a

    write_fake_cli(cli_b, "#!/bin/sh\nexit 0\n")
    unsafe_section = make_agent_section(permission="unsafe")
    digest_permission_changed = agent_identity(
        unsafe_section, prompt_seal, seal_program(cli_b)
    ).digest
    assert digest_permission_changed != digest_a


def test_environment_values_never_enter_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = write_prompt(tmp_path / "prompt.md")
    section = make_chat_section(proxy_env="EDITOR_PROXY")
    seal = seal_prompt(prompt, packaged=TREE_EDITOR_SYSTEM)

    monkeypatch.setenv("EDITOR_API_KEY", "first-secret-value")
    monkeypatch.setenv("EDITOR_PROXY", "http://proxy-one")
    digest_before = chat_identity(section, seal).digest

    monkeypatch.setenv("EDITOR_API_KEY", "totally-different-secret")
    monkeypatch.setenv("EDITOR_PROXY", "http://proxy-two")
    digest_after = chat_identity(section, seal).digest

    assert digest_before == digest_after


def test_seal_verify_detects_change_and_packaged_prompt_is_constant(tmp_path: Path) -> None:
    prompt = write_prompt(tmp_path / "prompt.md")
    seal = seal_prompt(prompt, packaged=TREE_EDITOR_SYSTEM)
    seal.verify()

    write_prompt(prompt, "changed contents\n")
    with pytest.raises(SealChanged):
        seal.verify()

    packaged_seal = seal_prompt(None, packaged=TREE_EDITOR_SYSTEM)
    packaged_seal.verify()
    assert packaged_seal.text() == TREE_EDITOR_SYSTEM
    assert packaged_seal.sha256 == hashlib.sha256(TREE_EDITOR_SYSTEM.encode()).hexdigest()


def test_seal_verify_reports_a_deleted_prompt_file_as_sealchanged(tmp_path: Path) -> None:
    prompt = write_prompt(tmp_path / "prompt.md")
    seal = seal_prompt(prompt, packaged=TREE_EDITOR_SYSTEM)

    prompt.unlink()

    with pytest.raises(SealChanged):
        seal.verify()
    with pytest.raises(SealChanged):
        seal.text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_seal_verify_reports_an_unreadable_program_as_sealchanged(tmp_path: Path) -> None:
    cli = write_fake_cli(tmp_path / "agent-cli")
    seal = seal_program(cli)

    cli.chmod(0o000)
    try:
        with pytest.raises(SealChanged):
            seal.verify()
    finally:
        cli.chmod(0o755)


def test_identity_values_are_identifiers() -> None:
    prompt_seal = FileSeal(path=None, sha256="0" * 64, packaged_text="x")
    section = make_chat_section()
    identity = chat_identity(section, prompt_seal)
    assert identity.kind == "chat"
    assert len(identity.digest) == 64

    assert CHAT_TOOL_SCHEMA == "chat_tools/1"

    payload = section_payload(section)
    assert "system_prompt" not in payload
    assert "budgets" in payload and isinstance(payload["budgets"], dict)


def test_a_declared_context_limit_changes_the_chat_identities() -> None:
    prompt_seal = FileSeal(path=None, sha256="0" * 64, packaged_text="x")
    default = make_chat_section()
    for raised in (
        make_chat_section(context_compact_chars=400_000),
        make_chat_section(context_message_max_chars=60_000),
    ):
        assert section_payload(raised) != section_payload(default)
        assert chat_identity(raised, prompt_seal) != chat_identity(default, prompt_seal)
        assert chat_reflector_identity(raised, prompt_seal) != chat_reflector_identity(
            default, prompt_seal
        )


def test_the_brief_version_is_part_of_both_in_process_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles import seals as editor_seal
    from evolution.tree.roles.editor.editor_brief import BRIEF_VERSION

    assert BRIEF_VERSION == "edit_brief/4"  # W19 Task 6: edit_brief/4
    prompt = seal_prompt(write_prompt(tmp_path / "prompt.md"), packaged=TREE_EDITOR_SYSTEM)
    program = seal_program(write_fake_cli(tmp_path / "cli"))
    chat_now = chat_identity(make_chat_section(), prompt).digest
    agent_now = agent_identity(make_agent_section(), prompt, program).digest
    expected_chat = hashlib.sha256(
        editor_seal.canonical_json(
            {
                "kind": "chat",
                "section": section_payload(make_chat_section()),
                "system_prompt_sha256": prompt.sha256,
                "tool_schema": CHAT_TOOL_SCHEMA,
                "brief_version": "edit_brief/4",  # W19 Task 6: edit_brief/4
            }
        )
    ).hexdigest()
    assert chat_now == expected_chat

    monkeypatch.setattr(editor_seal, "BRIEF_VERSION", "edit_brief/1")
    assert chat_identity(make_chat_section(), prompt).digest != chat_now
    assert agent_identity(make_agent_section(), prompt, program).digest != agent_now


def test_reflector_identities_differ_from_editor_identities(tmp_path: Path) -> None:
    chat_section = make_chat_section()
    agent_section = make_agent_section()
    prompt_seal = seal_prompt(write_prompt(tmp_path / "prompt.md"), packaged=TREE_EDITOR_SYSTEM)
    program_seal = seal_program(write_fake_cli(tmp_path / "cli"))

    assert (
        chat_reflector_identity(chat_section, prompt_seal).digest
        != chat_identity(chat_section, prompt_seal).digest
    )
    assert agent_reflector_identity(agent_section, prompt_seal, program_seal).kind == "agent_cli"


# -- #290 §4.2, C5: an undeclared `api_key_env` is left out, a declared one stays --

#: Digests computed at `d5daf1d5d`, before #290 Task 4 touched `section_payload`:
#: a section that declares its key keeps every one of them.
DECLARED_KEY_DIGESTS = {
    "editor": "b6a831ca0f2069258bba38fe3e5e5a1ddaa1eba187f0546391179a21ea8645e4",
    "editor_proxy": "6caa69d346762f7fb7930a064718cec20f0d57c295ecdf43dfd89ce936f7ddff",
    "reflector": "fdda19d6e2e6a7d3c6268367e95c5c1e7953c960aa5e12acd80aab48c1f4fc62",
    "editor_retry": "6caff07b6c91db3ab0b475e7c1ee4aad420a345c8a0c7db39bcd08113f4d2053",
    "reflector_retry": "b96cc03a11bbb9cd99798f1105051db34f8546524025a7fddb003959d3445e31",
}


def _retry_section(**overrides: object) -> ChatEditorSection:
    from evolution.tree.roles.role_call import RoleCallRetry
    from tests.tree.manifest.test_manifest_v2 import RETRY

    return make_chat_section(
        retry=RoleCallRetry.from_payload(RETRY), proxy_env="REFLECT_PROXY", **overrides
    )


def test_a_declared_key_keeps_every_chat_identity_digest() -> None:
    seal = FileSeal(path=None, sha256="0" * 64, packaged_text="x")
    assert {
        "editor": chat_identity(make_chat_section(), seal).digest,
        "editor_proxy": chat_identity(make_chat_section(proxy_env="EDITOR_PROXY"), seal).digest,
        "reflector": chat_reflector_identity(make_chat_section(), seal).digest,
        "editor_retry": chat_identity(_retry_section(), seal).digest,
        "reflector_retry": chat_reflector_identity(_retry_section(), seal).digest,
    } == DECLARED_KEY_DIGESTS
    assert section_payload(make_chat_section())["api_key_env"] == "EDITOR_API_KEY"


@pytest.mark.parametrize("build", [make_chat_section, _retry_section], ids=["plain", "retry"])
def test_an_undeclared_key_is_left_out_of_the_section_not_written_as_null(build) -> None:
    seal = FileSeal(path=None, sha256="0" * 64, packaged_text="x")
    declared = build()
    bare = build(api_key_env=None)
    payload = section_payload(bare)

    assert "api_key_env" not in payload
    assert payload == {k: v for k, v in section_payload(declared).items() if k != "api_key_env"}

    # A null member would hash differently from the omission C5 binds.
    def digest(section: dict[str, object]) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "kind": "chat",
                    "section": section,
                    "system_prompt_sha256": seal.sha256,
                    "tool_schema": CHAT_TOOL_SCHEMA,
                    "brief_version": BRIEF_VERSION,
                }
            )
        ).hexdigest()

    assert chat_identity(bare, seal).digest == digest(payload)
    assert chat_identity(bare, seal).digest != digest({**payload, "api_key_env": None})
    # Adding or removing the key changes both chat identities.
    assert chat_identity(bare, seal) != chat_identity(declared, seal)
    assert chat_reflector_identity(bare, seal) != chat_reflector_identity(declared, seal)


V15_CHAT_DIGEST = "b16e0df240df1f0d62e7496c717bf46c1138e56a828651010a01c5e08f6319ff"


def test_v15_chat_editor_identity_digest_is_unchanged():
    assert (
        chat_identity(make_chat_section(), seal_prompt(None, packaged="you are an editor\n")).digest
        == V15_CHAT_DIGEST
    )


def test_v16_chat_editor_identity_adds_plan_correction():
    assert (
        chat_identity(
            make_chat_section(),
            seal_prompt(None, packaged="you are an editor\n"),
            plan_correction=True,
        ).digest
        != V15_CHAT_DIGEST
    )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("chat", "41fa154f01133f98f0435e3c7d9eaf197cdc446bd68824a7b296199cf67f0f66"),
        ("agent", "234a5d8e0c8fe0c9525798a5203f3995979ce163d70186192b0ca410654c6887"),
        ("chat_reflector", "a3b7c297008f9b76458986fdf6f605bb2f7b95ac1a3de0ad45b5a2d7678d8f15"),
        ("agent_reflector", "e699eab194b250afd17befe76e5cbdde74a9e5f935a2952d1eead63bae81d1d7"),
    ],
)
def test_family15_role_identities_change_and_family14_identities_do_not(name, expected):
    # Captured from exact b12 base 02c2873f3 before editing the identities.
    prompt = FileSeal(None, "a" * 64, "prompt")
    program = FileSeal(Path("/unused"), "b" * 64)
    fn = {
        "chat": chat_identity,
        "agent": agent_identity,
        "chat_reflector": chat_reflector_identity,
        "agent_reflector": agent_reflector_identity,
    }[name]
    section = make_agent_section() if name.startswith("agent") else make_chat_section()
    args = (program,) if name.startswith("agent") else ()
    assert fn(section, prompt, *args).digest == expected
    assert fn(section, prompt, *args, family15=True).digest != expected


def test_v15_chat_identity_digests_unchanged_by_role_contracts():
    section = make_chat_section()
    prompt = FileSeal(None, "a" * 64, packaged_text="fixed")
    assert (
        chat_identity(section, prompt).digest
        == "41fa154f01133f98f0435e3c7d9eaf197cdc446bd68824a7b296199cf67f0f66"
    )
    assert (
        chat_reflector_identity(section, prompt).digest
        == "a3b7c297008f9b76458986fdf6f605bb2f7b95ac1a3de0ad45b5a2d7678d8f15"
    )


@pytest.mark.parametrize("role", ["editor", "reflector"])
def test_v16_chat_identities_carry_role_contracts_marker(role):
    from evolution.tree.roles import seals

    section = make_chat_section()
    prompt = FileSeal(None, "a" * 64, packaged_text="fixed")
    payload = {
        "kind": "chat",
        "section": section_payload(section),
        "system_prompt_sha256": "a" * 64,
        "role_contracts": "role_contracts/1",
    }
    if role == "editor":
        payload.update(
            tool_schema=seals.CHAT_TOOL_SCHEMA_V16, brief_version=seals.BRIEF_VERSION_V16
        )
        identity = chat_identity(section, prompt, family15=True)
    else:
        payload.update(
            tool_schema=seals.REFLECT_TOOL_SCHEMA_V16,
            request_version=seals.REFLECT_REQUEST_VERSION,
            reflect_brief="reflect_brief/6",
        )
        identity = chat_reflector_identity(section, prompt, family15=True)
    assert identity.digest == hashlib.sha256(canonical_json(payload)).hexdigest()
