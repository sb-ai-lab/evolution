"""Behavioral contracts for the manifest-to-kernel bindings and the corpus loader."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evolution.eval.child_env import _RUNTIME_ENV_KEYS
from evolution.llm.agent_backend import CURATED_ENV_NAMES
from evolution.prompts import TREE_AGENT_EDITOR_SYSTEM, TREE_EDITOR_SYSTEM
from evolution.tree.bindings import (
    BindingError,
    bound_child_environment,
    build_editor,
    build_environment,
    build_reflector,
    build_scorer,
    editor_environment_names,
    editor_environment_paths,
    editor_environment_values,
    editor_program_paths,
    reflector_environment_names,
    reflector_environment_paths,
    reflector_environment_values,
    reflector_program_paths,
)
from evolution.tree.execution.config import CommandSection, EnvironmentSection
from evolution.tree.execution.corpus import CorpusError, load_eval_corpus, load_reflection_corpus
from evolution.tree.execution.interaction import ScriptedEnvironment, StaticInputEnvironment
from evolution.tree.execution.journal_identity import corpus_digest
from evolution.tree.execution.output_admission import (
    FAMILY14_OUTPUT_ROOTS,
    OUTPUT_ROOTS,
    EvolveRefused,
    collect_output_root_findings,
    output_roots,
)
from evolution.tree.roles.config import (
    AgentCliEditorSection,
    ChatEditorSection,
    EditorSection,
    ReflectorSection,
)
from evolution.tree.roles.editor.agent_editor import AgentCliEditor
from evolution.tree.roles.editor.chat_editor import ChatCredentials, ChatEditor
from evolution.tree.roles.editor.subprocess_editor import SubprocessEditor
from evolution.tree.roles.reflector.chat_reflector import ChatReflector
from evolution.tree.roles.reflector.subprocess_reflector import SubprocessReflector
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.seals import chat_identity, seal_prompt


def write_corpus(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def scorer_section(tmp_path: Path) -> CommandSection:
    cwd = tmp_path / "scorer"
    cwd.mkdir()
    (cwd / "main.py").write_text("", encoding="utf-8")
    return CommandSection(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset(),
        timeout_seconds=5.0,
    )


def test_corpus_rows_become_eval_cases_in_order(tmp_path: Path) -> None:
    path = write_corpus(
        tmp_path,
        '{"case_id":"b","group_id":"g1","stratum":"s","public":{"text":"one"}}',
        "",
        '{"case_id":"a","group_id":"g2","stratum":"s","tags":["t"],'
        '"public":{"text":"two"},"private":{"k":1},"oracle":{"answer":"x"}}',
    )

    cases = load_eval_corpus(path)

    assert [case.case_id for case in cases] == ["b", "a"]
    assert cases[0].tags == ()
    assert dict(cases[0].private) == {}
    assert dict(cases[0].oracle) == {}
    assert dict(cases[0].public) == {"text": "one"}
    assert cases[1].tags == ("t",)
    assert cases[1].group_id == "g2"
    assert cases[1].oracle["answer"] == "x"


def test_reflection_metadata_is_separate_immutable_and_does_not_change_measurement_cases(
    tmp_path: Path,
) -> None:
    with_metadata = write_corpus(
        tmp_path,
        '{"case_id":"a","group_id":"g","stratum":"s","public":{"text":"one"},'
        '"reflection_metadata":{"family":"faq","nested":{"n":1}}}',
    )
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = write_corpus(
        plain_dir,
        '{"case_id":"a","group_id":"g","stratum":"s","public":{"text":"one"}}',
    )

    reflection = load_reflection_corpus(with_metadata)
    assert reflection.metadata["a"]["family"] == "faq"
    with pytest.raises(TypeError):
        reflection.metadata["a"] = {}  # type: ignore[index]
    assert corpus_digest(reflection.cases) == corpus_digest(load_eval_corpus(plain))
    assert reflection.metadata_digest != load_reflection_corpus(plain).metadata_digest
    with pytest.raises(CorpusError) as info:
        load_eval_corpus(with_metadata)
    assert (info.value.code, info.value.line) == ("unknown_key", 1)


@pytest.mark.parametrize(
    "metadata",
    [
        "[]",
        '{"x": NaN}',
        "{" + ",".join(f'"k{i}":{i}' for i in range(65)) + "}",
    ],
)
def test_reflection_metadata_refuses_nonfinite_and_bounded_invalid_values(
    tmp_path: Path, metadata: str
) -> None:
    path = write_corpus(
        tmp_path,
        '{"case_id":"a","group_id":"g","stratum":"s","public":{},'
        '"reflection_metadata":' + metadata + "}",
    )
    with pytest.raises(CorpusError) as info:
        load_reflection_corpus(path)
    assert (info.value.code, info.value.line) in {("bad_type", 1), ("bad_value", 1)}


@pytest.mark.parametrize(
    ("lines", "code", "line"),
    [
        (("{",), "not_json", 1),
        (("[]",), "not_object", 1),
        (
            ('{"case_id":"a","group_id":"g","stratum":"s","public":{},"extra":1}',),
            "unknown_key",
            1,
        ),
        (('{"case_id":"a","group_id":"g","stratum":"s"}',), "missing_key", 1),
        (('{"case_id":"a","group_id":"g","stratum":"s","public":[]}',), "bad_type", 1),
        (
            (
                '{"case_id":"a","group_id":"g","stratum":"s","public":{}}',
                '{"case_id":"a","group_id":"g","stratum":"s","public":{}}',
            ),
            "duplicate_case_id",
            2,
        ),
        ((), "empty_corpus", 0),
    ],
)
def test_corpus_errors_carry_line_numbers(
    tmp_path: Path, lines: tuple[str, ...], code: str, line: int
) -> None:
    with pytest.raises(CorpusError) as info:
        load_eval_corpus(write_corpus(tmp_path, *lines))

    assert (info.value.code, info.value.line) == (code, line)
    assert str(info.value) == f"{code} at line {line}"


def test_corpus_blank_row_values_are_bad_type(tmp_path: Path) -> None:
    path = write_corpus(tmp_path, '{"case_id":" ","group_id":"g","stratum":"s","public":{}}')

    with pytest.raises(CorpusError) as info:
        load_eval_corpus(path)

    assert (info.value.code, info.value.line) == ("bad_type", 1)


def test_corpus_that_cannot_be_read_is_unreadable_without_its_path(tmp_path: Path) -> None:
    """A `corpus.path` typo must refuse, not raise an `OSError` naming the path."""

    absent = tmp_path / "absent" / "corpus.jsonl"

    with pytest.raises(CorpusError) as info:
        load_eval_corpus(absent)

    assert (info.value.code, info.value.line) == ("unreadable", 0)
    assert str(info.value) == "unreadable at line 0"
    assert str(tmp_path) not in str(info.value)


def test_corpus_path_that_is_a_directory_is_unreadable(tmp_path: Path) -> None:
    directory = tmp_path / "corpus.jsonl"
    directory.mkdir()

    with pytest.raises(CorpusError) as info:
        load_eval_corpus(directory)

    assert (info.value.code, info.value.line) == ("unreadable", 0)
    assert str(tmp_path) not in str(info.value)


def test_corpus_that_is_not_utf8_is_unreadable(tmp_path: Path) -> None:
    """A decode failure is a `ValueError`, so it needs its own catch, not `OSError`'s."""

    path = tmp_path / "corpus.jsonl"
    path.write_bytes(b'{"case_id":"\xff\xfe","group_id":"g","stratum":"s","public":{}}\n')

    with pytest.raises(CorpusError) as info:
        load_eval_corpus(path)

    assert (info.value.code, info.value.line) == ("unreadable", 0)
    assert str(tmp_path) not in str(info.value)


def test_build_environment_kinds() -> None:
    static = build_environment(EnvironmentSection("static", "prompt"))
    scripted = build_environment(EnvironmentSection("scripted"))

    assert isinstance(static, StaticInputEnvironment)
    assert static.input_text_key == "prompt"
    assert isinstance(scripted, ScriptedEnvironment)


def test_build_scorer_binds_and_redacts(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    section = scorer_section(tmp_path)

    scorer = build_scorer(section, seed_root=seed, run_dir=tmp_path / "run")

    assert scorer.trace_root == (tmp_path / "run" / "scorer-traces").resolve()
    assert scorer.argv == (sys.executable, "main.py")
    assert scorer.timeout_seconds == 5.0
    assert scorer.command.seed_root == seed.resolve()
    assert not (tmp_path / "run").exists()  # nothing written at bind time

    missing = replace(section, cwd=tmp_path / "absent")
    with pytest.raises(BindingError) as info:
        build_scorer(missing, seed_root=seed, run_dir=tmp_path / "run")
    assert (info.value.code, info.value.path) == ("scorer_invalid", "scorer.cwd")
    assert str(info.value) == "scorer_invalid at scorer.cwd"
    assert str(tmp_path) not in str(info.value)
    assert str(tmp_path) in info.value.detail  # kept private, never in str()

    inside = replace(section, cwd=seed)
    with pytest.raises(BindingError) as info:
        build_scorer(inside, seed_root=seed, run_dir=tmp_path / "run")
    assert (info.value.code, info.value.path) == ("program_inside_seed", "scorer")
    assert str(info.value) == "program_inside_seed at scorer"


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("argv", (), "scorer.argv"),
        ("argv", ("definitely-not-a-real-program",), "scorer.argv"),
        ("argv", (sys.executable, "-m", "w1_any_module"), "scorer.argv"),
        ("dependency_paths", (Path("absent-dependency"),), "scorer.dependency_paths"),
        ("allowed_env", frozenset({"BAD=NAME"}), "scorer.allowed_env"),
        ("timeout_seconds", 0.0, "scorer.timeout_seconds"),
        ("timeout_seconds", None, "scorer.timeout_seconds"),
    ],
)
def test_build_scorer_maps_each_defect_to_its_field(
    tmp_path: Path, field: str, value: object, path: str
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    section = replace(scorer_section(tmp_path), **{field: value})

    with pytest.raises(BindingError) as info:
        build_scorer(section, seed_root=seed, run_dir=tmp_path / "run")

    assert (info.value.code, info.value.path) == ("scorer_invalid", path)


def editor_section(tmp_path: Path) -> CommandSection:
    cwd = tmp_path / "editor"
    cwd.mkdir()
    (cwd / "main.py").write_text("", encoding="utf-8")
    return CommandSection(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset(),
        timeout_seconds=None,  # the editor has no timeout of its own (spec §4)
    )


def test_build_editor_binds_and_redacts(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    section = editor_section(tmp_path)

    editor = build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ={})

    assert editor.log_root == (tmp_path / "run" / "editor-logs").resolve()
    assert editor.binding.argv == (sys.executable, "main.py")
    assert editor.binding.seed_root == seed.resolve()
    assert editor.identity.kind == "subprocess"
    assert editor.identity.digest == editor.binding.identity_digest()
    assert not (tmp_path / "run").exists()  # nothing written at bind time

    missing = replace(section, cwd=tmp_path / "absent")
    with pytest.raises(BindingError) as info:
        build_editor(missing, seed_root=seed, run_dir=tmp_path / "run", environ={})
    assert (info.value.code, info.value.path) == ("editor_invalid", "editor.cwd")
    assert str(info.value) == "editor_invalid at editor.cwd"
    assert str(tmp_path) not in str(info.value)
    assert str(tmp_path) in info.value.detail  # kept private, never in str()

    inside = replace(section, cwd=seed)
    with pytest.raises(BindingError) as info:
        build_editor(inside, seed_root=seed, run_dir=tmp_path / "run", environ={})
    assert (info.value.code, info.value.path) == ("program_inside_seed", "editor")
    assert str(info.value) == "program_inside_seed at editor"


def test_build_editor_ignores_a_scorer_style_timeout(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    section = replace(editor_section(tmp_path), timeout_seconds=5.0)

    editor = build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ={})

    # The editor's deadline is C1's (ADR-0001); a declared timeout is not its
    # own and never reaches its identity.
    assert editor.identity.digest == editor.binding.identity_digest()


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("argv", (), "editor.argv"),
        ("argv", ("definitely-not-a-real-program",), "editor.argv"),
        ("argv", (sys.executable, "-m", "w1_any_module"), "editor.argv"),
        ("dependency_paths", (Path("absent-dependency"),), "editor.dependency_paths"),
        ("allowed_env", frozenset({"BAD=NAME"}), "editor.allowed_env"),
    ],
)
def test_build_editor_maps_each_defect_to_its_field(
    tmp_path: Path, field: str, value: object, path: str
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    section = replace(editor_section(tmp_path), **{field: value})

    with pytest.raises(BindingError) as info:
        build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ={})

    assert (info.value.code, info.value.path) == ("editor_invalid", path)


def test_build_scorer_drops_a_pythonpath_entry_under_the_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run directory holds the Attempt workspaces, so it is an excluded root too."""

    seed = tmp_path / "seed"
    seed.mkdir()
    run_dir = tmp_path / "run"
    inside_entry = str(run_dir / "artifacts")
    outside_entry = str(tmp_path / "libs")
    monkeypatch.setenv("PYTHONPATH", inside_entry + os.pathsep + outside_entry)
    section = scorer_section(tmp_path)

    scorer = build_scorer(section, seed_root=seed, run_dir=run_dir)

    assert scorer.command.dropped_pythonpath == (inside_entry,)
    assert scorer.command.child_env["PYTHONPATH"] == outside_entry
    assert not run_dir.exists()  # still nothing written at bind time


def test_build_editor_drops_a_pythonpath_entry_under_the_seed(tmp_path: Path) -> None:
    """The editor is bound through the same guard as the scorer, over `environ`."""

    seed = tmp_path / "seed"
    seed.mkdir()
    inside_entry = str(seed / "lib")
    section = editor_section(tmp_path)

    editor = build_editor(
        section, seed_root=seed, run_dir=tmp_path / "run", environ={"PYTHONPATH": inside_entry}
    )

    assert editor.binding.dropped_pythonpath == (inside_entry,)
    assert "PYTHONPATH" not in editor.binding.child_env


def test_every_subprocess_role_binds_the_environment_it_is_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subprocess_reflector_section: ReflectorSection,
) -> None:
    """The scorer, a `subprocess` editor and a `subprocess` Reflector bind `environ`.

    `evolve` hands every builder the one snapshot that also produced the run's
    secret set. A command binding that read the process environment instead
    could spawn a child with a value the redaction set never saw -- the
    divergence the snapshot exists to rule out, and one the `chat` and
    `agent_cli` kinds were already free of.
    """

    seed = tmp_path / "seed"
    seed.mkdir()
    run_dir = tmp_path / "run"
    monkeypatch.setenv("W1_TOKEN", "ambient-value")
    environ = {"W1_TOKEN": "snapshot-value"}
    forwarded = frozenset({"W1_TOKEN"})
    reflector_role = replace(subprocess_reflector_section.role, allowed_env=forwarded)

    scorer = build_scorer(
        replace(scorer_section(tmp_path), allowed_env=forwarded),
        seed_root=seed,
        run_dir=run_dir,
        environ=environ,
    )
    editor = build_editor(
        replace(editor_section(tmp_path), allowed_env=forwarded),
        seed_root=seed,
        run_dir=run_dir,
        environ=environ,
    )
    reflector = build_reflector(
        replace(subprocess_reflector_section, role=reflector_role),
        seed_root=seed,
        run_dir=run_dir,
        environ=environ,
    )

    assert scorer.command.child_env == {"W1_TOKEN": "snapshot-value"}
    assert isinstance(editor, SubprocessEditor)
    assert editor.binding.child_env == {"W1_TOKEN": "snapshot-value"}
    assert isinstance(reflector, SubprocessReflector)
    assert reflector.binding.child_env == {"W1_TOKEN": "snapshot-value"}


# --------------------------------------------------------------------------
# The `chat` and `agent_cli` dispatch (E1-editor design §3, §9)
# --------------------------------------------------------------------------


def chat_section(**kw: Any) -> ChatEditorSection:
    kw.setdefault("base_url", "https://gw.example/v1")
    return ChatEditorSection(model="m", api_key_env="E1_KEY", **kw)


def agent_section(tmp_path: Path, **kw: Any) -> AgentCliEditorSection:
    if "agent_bin" not in kw:
        binary = tmp_path / "codex-bin"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        kw["agent_bin"] = binary
    return AgentCliEditorSection(model="codex", **kw)


def test_build_editor_dispatches_by_section_type(tmp_path: Path) -> None:
    """One entry point, three bound editors; `subprocess` is unchanged (rule R1)."""

    seed = tmp_path / "seed"
    seed.mkdir()
    run_dir = tmp_path / "run"
    environ = {"E1_KEY": "sekret-key-value", "PATH": "/nowhere"}

    command = build_editor(
        editor_section(tmp_path), seed_root=seed, run_dir=run_dir, environ=environ
    )
    assert isinstance(command, SubprocessEditor)
    assert command.identity.kind == "subprocess"
    assert command.identity.digest == command.binding.identity_digest()

    chat = build_editor(
        chat_section(), seed_root=seed, run_dir=run_dir, environ=environ, secrets=("sekret",)
    )
    assert isinstance(chat, ChatEditor)
    assert chat.identity.kind == "chat"
    assert chat.identity == chat_identity(chat.section, chat.prompt)
    assert chat.credentials == ChatCredentials("sekret-key-value", None)
    assert chat.prompt.sha256 == seal_prompt(None, packaged=TREE_EDITOR_SYSTEM).sha256
    assert chat.log_root == (run_dir / "editor-logs")
    assert chat.secrets == ("sekret",)

    section = agent_section(tmp_path)
    agent = build_editor(section, seed_root=seed, run_dir=run_dir, environ=environ)
    assert isinstance(agent, AgentCliEditor)
    assert agent.identity.kind == "agent_cli"
    assert agent.program.path == section.agent_bin
    assert agent.prompt.sha256 == seal_prompt(None, packaged=TREE_AGENT_EDITOR_SYSTEM).sha256
    assert agent.log_root == (run_dir / "editor-logs")

    assert not run_dir.exists()  # nothing written at bind time, whatever the kind


_RETRY = RoleCallRetry(
    max_attempts=3,
    total_budget_seconds=30,
    initial_seconds=0,
    multiplier=1,
    max_seconds=0,
)


def test_build_editor_role_resilience_is_off_by_default_and_wired_when_asked(
    tmp_path: Path,
) -> None:
    """`build_editor`'s own `role_resilience` (PDR-0019/ADR-0023) reaches
    `ChatEditor` unchanged; `evolve` is the only caller that derives it from
    `manifest.schema`, so this is the wiring, not the schema selection."""

    seed = tmp_path / "seed"
    seed.mkdir()
    run_dir = tmp_path / "run"
    environ = {"E1_KEY": "sekret-key-value"}

    off = build_editor(chat_section(), seed_root=seed, run_dir=run_dir, environ=environ)
    assert off.role_resilience is False

    on = build_editor(
        chat_section(retry=_RETRY),
        seed_root=seed,
        run_dir=run_dir,
        environ=environ,
        role_resilience=True,
    )
    assert on.role_resilience is True


def test_editor_env_missing_for_key_proxy_and_allowed_env(tmp_path: Path) -> None:
    """Absent or empty: an in-process editor cannot call its endpoint without one."""

    seed = tmp_path / "seed"
    seed.mkdir()
    cases: list[tuple[EditorSection, dict[str, str], str]] = [
        (chat_section(), {}, "editor.api_key_env"),
        (chat_section(), {"E1_KEY": ""}, "editor.api_key_env"),
        (
            chat_section(proxy_env="E1_PROXY"),
            {"E1_KEY": "sekret-key-value"},
            "editor.proxy_env",
        ),
        (
            chat_section(proxy_env="E1_PROXY"),
            {"E1_KEY": "sekret-key-value", "E1_PROXY": ""},
            "editor.proxy_env",
        ),
        (
            agent_section(tmp_path, allowed_env=frozenset({"E1_FORWARDED"})),
            {},
            "editor.allowed_env",
        ),
        (
            agent_section(tmp_path, allowed_env=frozenset({"E1_FORWARDED"})),
            {"E1_FORWARDED": ""},
            "editor.allowed_env",
        ),
    ]
    for section, environ, path in cases:
        with pytest.raises(EvolveRefused) as refusal:
            editor_environment_values(section, environ)
        assert (refusal.value.code, refusal.value.path) == ("editor_env_missing", path)
        assert str(refusal.value) == f"editor_env_missing at {path}"

        with pytest.raises(EvolveRefused):
            build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ=environ)


def test_an_invalid_proxy_url_is_editor_invalid(tmp_path: Path) -> None:
    """The dormant editor's proxy rules, kept: HTTP(S), a hostname, no query."""

    seed = tmp_path / "seed"
    seed.mkdir()
    section = chat_section(proxy_env="E1_PROXY")
    invalid = (
        "socks5://proxy.example:1080",  # neither HTTP nor HTTPS
        "https:///v1",  # no hostname
        "https://p.example/?a=b",  # a query
        "https://p.example/#f",  # a fragment
        "http://p.example:3128",  # plaintext to a host that is not loopback
        "https://has space:3128",  # whitespace in the hostname
        "https://p.example:3128\n",  # a control character
    )
    for value in invalid:
        environ = {"E1_KEY": "sekret-key-value", "E1_PROXY": value}
        with pytest.raises(BindingError) as info:
            build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ=environ)
        assert (info.value.code, info.value.path) == ("editor_invalid", "editor.proxy_env")
        assert value.strip() not in str(info.value)

        # The environment step refuses it too, and refuses it first: that is the
        # step `evolve` runs before `check_path_separation` (design §9).
        with pytest.raises(BindingError) as info:
            editor_environment_values(section, environ)
        assert (info.value.code, info.value.path) == ("editor_invalid", "editor.proxy_env")

    # Userinfo is admitted, as it was for `EVO_EDITOR_PROXY`: an authenticating
    # proxy needs it, and the whole value is a secret of the run either way.
    for value in ("https://user:pass@proxy.example:3128", "http://127.0.0.1:3128"):
        good = {"E1_KEY": "sekret-key-value", "E1_PROXY": value}
        editor = build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ=good)
        assert isinstance(editor, ChatEditor)
        assert editor.credentials.proxy_url == value


def test_editor_program_missing_when_no_cli_on_bound_path(tmp_path: Path) -> None:
    """No `agent_bin` and nothing on the bound `PATH`: refused before any write."""

    seed = tmp_path / "seed"
    seed.mkdir()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    section = agent_section(tmp_path, agent_bin=None)
    environ = {"PATH": str(empty)}

    with pytest.raises(EvolveRefused) as refusal:
        build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ=environ)
    assert (refusal.value.code, refusal.value.path) == (
        "editor_program_missing",
        "editor.agent_bin",
    )

    with pytest.raises(EvolveRefused) as refusal:
        editor_program_paths(section, environ)
    assert refusal.value.code == "editor_program_missing"

    # The same section resolves once a `codex` is on the bound `PATH`, and the
    # resolved executable -- not the name -- is what the seal and the paths carry.
    binary = empty / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    editor = build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ=environ)
    assert isinstance(editor, AgentCliEditor)
    assert editor.program.path == binary
    assert editor_program_paths(section, environ) == (binary,)
    assert not (tmp_path / "run").exists()


def test_bound_child_environment_safe_and_unsafe(tmp_path: Path) -> None:
    """One mapping is the authority for the spawn and for the redaction (§9)."""

    environ = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "HOME": "/home/op",
        "OPENAI_API_KEY": "curated-key-value",
        "E1_FORWARDED": "forwarded-value",
        "UNRELATED_TOKEN": "unrelated-value",
    }
    section = agent_section(tmp_path, allowed_env=frozenset({"E1_FORWARDED"}))

    safe = bound_child_environment(section, environ)
    expected = {
        name: value
        for name, value in environ.items()
        if name in (_RUNTIME_ENV_KEYS | CURATED_ENV_NAMES("codex") | {"E1_FORWARDED"})
    }
    assert safe == expected
    assert "UNRELATED_TOKEN" not in safe

    unsafe = bound_child_environment(replace(section, permission="unsafe"), environ)
    assert unsafe == environ
    assert unsafe is not environ  # a copy: the binding never aliases the operator's


def test_secret_values_derive_from_the_bound_environment(tmp_path: Path) -> None:
    """A curated credential the CLI receives is a secret of the run, declared or not."""

    environ = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "curated-key-value",
        "E1_FORWARDED": "forwarded-value",
        "UNRELATED_TOKEN": "unrelated-value",
    }
    section = agent_section(tmp_path, allowed_env=frozenset({"E1_FORWARDED"}))

    values = editor_environment_values(section, environ)

    assert sorted(values) == ["curated-key-value", "forwarded-value"]
    # `PATH` is a runtime name, not a credential; `UNRELATED_TOKEN` never reaches
    # the CLI in `safe` mode, so redacting it would be a claim about the wrong run.
    assert "/usr/bin" not in values
    assert "unrelated-value" not in values

    unsafe = editor_environment_values(replace(section, permission="unsafe"), environ)
    assert "unrelated-value" in unsafe  # the cost of the operator's declaration


def test_editor_environment_values_and_names_by_kind(tmp_path: Path) -> None:
    """Chat carries key and proxy; `subprocess` keeps W1's forwarded values."""

    environ = {"E1_KEY": "sekret-key-value", "E1_PROXY": "https://proxy.example:3128"}
    chat = chat_section(proxy_env="E1_PROXY")

    assert editor_environment_values(chat, environ) == (
        "sekret-key-value",
        "https://proxy.example:3128",
    )
    assert editor_environment_names(chat) == frozenset({"E1_KEY", "E1_PROXY"})
    assert editor_environment_names(chat_section()) == frozenset({"E1_KEY"})

    command = replace(editor_section(tmp_path), allowed_env=frozenset({"E1_KEY", "E1_ABSENT"}))
    assert editor_environment_values(command, environ) == ("sekret-key-value",)
    assert editor_environment_names(command) == frozenset({"E1_KEY", "E1_ABSENT"})
    assert editor_environment_names(agent_section(tmp_path)) == frozenset()


def test_editor_program_paths_are_the_declared_files(tmp_path: Path) -> None:
    """What path separation must see: the cwd, the prompt, the resolved CLI."""

    prompt = tmp_path / "prompt.md"
    prompt.write_text("be brief\n", encoding="utf-8")

    command = editor_section(tmp_path)
    assert editor_program_paths(command, {}) == (command.cwd,)
    assert editor_program_paths(chat_section(), {}) == ()
    assert editor_program_paths(chat_section(system_prompt=prompt), {}) == (prompt,)

    section = agent_section(tmp_path, system_prompt=prompt)
    assert section.agent_bin is not None
    assert editor_program_paths(section, {}) == (prompt, section.agent_bin)


# --------------------------------------------------------------------------
# The Reflector's own bindings (v2 Reflector X1 design §3.3): `build_reflector`
# is `build_editor`'s faithful mirror, and `OUTPUT_ROOTS` grows to hold its logs
# and its environment-documentation snapshot.
# --------------------------------------------------------------------------


@pytest.fixture
def subprocess_reflector_section(tmp_path: Path) -> ReflectorSection:
    cwd = tmp_path / "reflector"
    cwd.mkdir()
    (cwd / "reflect.py").write_text("", encoding="utf-8")
    role = CommandSection(
        argv=(sys.executable, "reflect.py"),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset(),
        timeout_seconds=None,
    )
    return ReflectorSection(role=role, environment_docs=None, probe=None)


@pytest.fixture
def chat_reflector_section() -> ReflectorSection:
    role = ChatEditorSection(model="m", base_url="https://gw.example/v1", api_key_env="REFLECT_KEY")
    return ReflectorSection(role=role, environment_docs=None, probe=None)


def test_output_roots_include_the_reflector_roots():
    assert OUTPUT_ROOTS == (
        "scorer-traces",
        "editor-logs",
        "reflector-logs",
        "environment-docs",
    )


def test_only_family_14_owns_the_log_roots():
    """H10 review r1 item 2: families 13 and older keep exactly their four roots."""

    from evolution.tree.manifest.manifest_v2 import SCHEMA_V15

    assert FAMILY14_OUTPUT_ROOTS == (*OUTPUT_ROOTS, "connection-logs", "logs")
    assert output_roots(SCHEMA_V15) == FAMILY14_OUTPUT_ROOTS
    for revision in range(1, 15):
        assert output_roots(f"evolution-v2-manifest/{revision}") == OUTPUT_ROOTS


@pytest.mark.parametrize("root_name", ["connection-logs", "logs"])
def test_a_group_readable_log_root_is_a_hygiene_finding(tmp_path: Path, root_name: str) -> None:
    """H10 and ADR-0036: both log roots admit specific hygiene decisions."""

    from evolution.tree.evolve import run_dir_state
    from evolution.tree.execution.authorizations import hygiene_instance_digest

    run = tmp_path / "run"
    run.mkdir()
    logs = run / root_name
    logs.mkdir()
    os.chmod(logs, 0o750)
    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(run, roots=FAMILY14_OUTPUT_ROOTS)
    assert (refusal.value.code, refusal.value.path) == ("run_dir_unsafe_entry", root_name)
    expected = hygiene_instance_digest(root_name, "not owner-only", "0o750")
    assert refusal.value.instance_digest == expected
    assert run_dir_state(run, admitted={expected}, roots=FAMILY14_OUTPUT_ROOTS) == "new"
    (finding,) = collect_output_root_findings(run, (), roots=FAMILY14_OUTPUT_ROOTS)
    assert finding.instance_digest == expected
    os.chmod(logs, 0o700)
    assert run_dir_state(run, roots=FAMILY14_OUTPUT_ROOTS) == "new"


@pytest.mark.parametrize("root_name", ["connection-logs", "logs"])
def test_log_roots_are_foreign_to_the_older_roots(tmp_path: Path, root_name: str) -> None:
    """H10 review r1 item 2: under the four roots both log entries are unguarded.

    A new run directory holding it is somebody else's (`run_dir_not_campaign`,
    as before H10), and beside a campaign journal it is never judged, so a
    group-readable one is no `run_dir_unsafe_entry`.
    """

    from evolution.tree.evolve import run_dir_state

    run = tmp_path / "run"
    run.mkdir()
    logs = run / root_name
    logs.mkdir()
    os.chmod(logs, 0o750)
    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(run)
    assert (str(refusal.value), refusal.value.instance_digest) == ("run_dir_not_campaign", None)
    assert collect_output_root_findings(run, ()) == ()
    (run / "campaign.jsonl").write_text("{}\n", encoding="utf-8")
    assert run_dir_state(run) == "resume"


def test_build_reflector_subprocess_binds_like_the_editor(
    tmp_path: Path, subprocess_reflector_section: ReflectorSection
):
    reflector = build_reflector(
        subprocess_reflector_section,
        seed_root=tmp_path / "seed",
        run_dir=tmp_path / "run",
        environ={"PATH": "/usr/bin:/bin"},
    )
    assert isinstance(reflector, SubprocessReflector)
    assert reflector.identity.kind == "subprocess"
    assert reflector.identity.digest == reflector.binding.identity_digest()
    assert reflector.log_root == (tmp_path / "run" / "reflector-logs").resolve()
    assert reflector_environment_names(subprocess_reflector_section) == frozenset()
    assert reflector_environment_values(subprocess_reflector_section, {}) == ()
    assert reflector_program_paths(subprocess_reflector_section, {"PATH": "/usr/bin:/bin"})
    assert not (tmp_path / "run").exists()  # nothing written at bind time


def test_build_reflector_chat_requires_its_key(
    tmp_path: Path, chat_reflector_section: ReflectorSection
):
    with pytest.raises(EvolveRefused) as info:
        build_reflector(
            chat_reflector_section,
            seed_root=tmp_path / "seed",
            run_dir=tmp_path / "run",
            environ={},
        )
    assert (info.value.code, info.value.path) == ("reflector_env_missing", "reflector.api_key_env")


def test_build_reflector_chat_binds_with_its_key(
    tmp_path: Path, chat_reflector_section: ReflectorSection
):
    reflector = build_reflector(
        chat_reflector_section,
        seed_root=tmp_path / "seed",
        run_dir=tmp_path / "run",
        environ={"REFLECT_KEY": "sekret-key-value"},
        secrets=("sekret",),
    )
    assert isinstance(reflector, ChatReflector)
    assert reflector.identity.kind == "chat"
    assert reflector.credentials == ChatCredentials("sekret-key-value", None)
    assert reflector.log_root == (tmp_path / "run" / "reflector-logs")
    assert reflector.secrets == ("sekret",)
    assert reflector_program_paths(chat_reflector_section, {}) == ()


@pytest.mark.parametrize("kind", ["chat", "agent_cli"])
@pytest.mark.parametrize("model_grouping", [False, True])
@pytest.mark.parametrize("declared", [False, True])
def test_reflector_prompt_selection_preserves_base_v3_and_declared_bytes(
    tmp_path, kind, model_grouping, declared
):
    from evolution import prompts

    prompt = tmp_path / "reflector.md"
    prompt.write_bytes(b"Declared reflector prompt.\n\n")
    role = (
        chat_section(system_prompt=prompt if declared else None)
        if kind == "chat"
        else agent_section(tmp_path, system_prompt=prompt if declared else None)
    )
    section = ReflectorSection(role=role, environment_docs=None, probe=None)
    kwargs = dict(
        seed_root=tmp_path / "seed",
        run_dir=tmp_path / "run",
        environ={"E1_KEY": "sekret-key-value"},
    )
    reflector = (
        build_reflector(section, model_grouping_family=True, **kwargs)
        if model_grouping
        else build_reflector(section, **kwargs)
    )
    name = "TREE_REFLECTOR_SYSTEM" if kind == "chat" else "TREE_AGENT_REFLECTOR_SYSTEM"
    packaged = getattr(prompts, name + ("_V3" if model_grouping else ""))
    assert reflector.prompt == seal_prompt(prompt if declared else None, packaged=packaged)
    assert not (tmp_path / "run").exists()


def test_build_reflector_role_resilience_is_off_by_default_and_wired_when_asked(
    tmp_path: Path,
) -> None:
    """`build_reflector`'s own `role_resilience`, the Reflector's half of the
    Editor's own wiring test above."""

    role = ChatEditorSection(
        model="m", base_url="https://gw.example/v1", api_key_env="REFLECT_KEY", retry=_RETRY
    )
    section = ReflectorSection(role=role, environment_docs=None, probe=None)
    environ = {"REFLECT_KEY": "sekret-key-value"}

    on = build_reflector(
        section,
        seed_root=tmp_path / "seed",
        run_dir=tmp_path / "run",
        environ=environ,
        role_resilience=True,
    )
    assert on.role_resilience is True

    off_section = ReflectorSection(
        role=ChatEditorSection(
            model="m", base_url="https://gw.example/v1", api_key_env="REFLECT_KEY"
        ),
        environment_docs=None,
        probe=None,
    )
    off = build_reflector(
        off_section, seed_root=tmp_path / "seed", run_dir=tmp_path / "run", environ=environ
    )
    assert off.role_resilience is False


def test_build_reflector_bounds_repair_is_off_by_default_and_wired_when_asked(
    tmp_path: Path,
) -> None:
    """#180: `bounds_repair` reaches a chat Reflector only when asked."""

    section = ReflectorSection(
        role=ChatEditorSection(
            model="m", base_url="https://gw.example/v1", api_key_env="REFLECT_KEY"
        ),
        environment_docs=None,
        probe=None,
    )
    environ = {"REFLECT_KEY": "sekret-key-value"}
    kwargs: dict[str, Any] = {
        "seed_root": tmp_path / "seed",
        "run_dir": tmp_path / "run",
        "environ": environ,
    }

    assert build_reflector(section, **kwargs).bounds_repair is False
    assert build_reflector(section, bounds_repair=True, **kwargs).bounds_repair is True


def test_reflector_program_paths_include_the_declared_environment_docs(
    tmp_path: Path, subprocess_reflector_section: ReflectorSection
):
    docs = tmp_path / "docs"
    docs.mkdir()
    section = replace(subprocess_reflector_section, environment_docs=docs)
    assert reflector_program_paths(section, {}) == (section.role.cwd, docs)


def test_reflector_environment_values_reports_its_own_vocabulary(
    chat_reflector_section: ReflectorSection,
):
    """A missing reflector key must not read as an editor's own refusal."""

    with pytest.raises(EvolveRefused) as info:
        reflector_environment_values(chat_reflector_section, {})
    assert (info.value.code, info.value.path) == ("reflector_env_missing", "reflector.api_key_env")
    assert str(info.value) == "reflector_env_missing at reflector.api_key_env"


def test_reflector_environment_values_reports_a_bad_proxy_under_its_own_vocabulary(
    chat_reflector_section: ReflectorSection,
):
    section = replace(
        chat_reflector_section, role=replace(chat_reflector_section.role, proxy_env="REFLECT_PROXY")
    )
    environ = {"REFLECT_KEY": "sekret-key-value", "REFLECT_PROXY": "ftp://bad.example"}

    with pytest.raises(BindingError) as info:
        reflector_environment_values(section, environ)
    assert (info.value.code, info.value.path) == ("reflector_invalid", "reflector.proxy_env")
    assert str(info.value) == "reflector_invalid at reflector.proxy_env"


# -- #290: a chat role without `api_key_env` is unauthenticated, not refused ---


def _unauthenticated(proxy_env: str | None = None) -> ChatEditorSection:
    return ChatEditorSection(
        model="m", base_url="https://gw.example/v1", api_key_env=None, proxy_env=proxy_env
    )


@pytest.mark.parametrize("proxy", [False, True])
def test_an_undeclared_editor_key_contributes_no_value_and_no_refusal(
    tmp_path: Path, proxy: bool
) -> None:
    """Design §4.1, C6: no `editor_env_missing`, no key value in the secret set,
    `ChatCredentials.api_key is None`; a declared proxy still contributes and
    binds. Paths stay index for index with values (#287)."""

    proxy_url = "https://proxy.example:3128"
    section = _unauthenticated("E1_PROXY" if proxy else None)
    environ = {"E1_PROXY": proxy_url, "E1_KEY": "sekret-key-value"}
    expected = (proxy_url,) if proxy else ()
    assert editor_environment_values(section, environ) == expected
    assert editor_environment_paths(section, environ) == (("editor.proxy_env",) if proxy else ())
    assert editor_environment_names(section) == (frozenset({"E1_PROXY"}) if proxy else frozenset())
    seed = tmp_path / "seed"
    seed.mkdir()
    editor = build_editor(section, seed_root=seed, run_dir=tmp_path / "run", environ=environ)
    assert isinstance(editor, ChatEditor)
    assert editor.credentials == ChatCredentials(None, proxy_url if proxy else None)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("proxy", [False, True])
def test_an_undeclared_reflector_key_contributes_no_value_and_no_refusal(
    tmp_path: Path, proxy: bool
) -> None:
    proxy_url = "https://proxy.example:3128"
    section = ReflectorSection(
        role=_unauthenticated("REFLECT_PROXY" if proxy else None),
        environment_docs=None,
        probe=None,
    )
    environ = {"REFLECT_PROXY": proxy_url}
    assert reflector_environment_values(section, environ) == ((proxy_url,) if proxy else ())
    assert reflector_environment_paths(section, environ) == (
        ("reflector.proxy_env",) if proxy else ()
    )
    reflector = build_reflector(
        section, seed_root=tmp_path / "seed", run_dir=tmp_path / "run", environ=environ
    )
    assert isinstance(reflector, ChatReflector)
    assert reflector.credentials == ChatCredentials(None, proxy_url if proxy else None)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("environ", [{}, {"REFLECT_KEY": ""}], ids=["absent", "empty"])
def test_a_declared_reflector_key_still_needs_a_value(
    tmp_path: Path, chat_reflector_section: ReflectorSection, environ: dict[str, str]
) -> None:
    """G3: a declared `api_key_env` keeps today's refusal, absent and empty alike."""

    for call in (
        lambda: reflector_environment_values(chat_reflector_section, environ),
        lambda: build_reflector(
            chat_reflector_section,
            seed_root=tmp_path / "seed",
            run_dir=tmp_path / "run",
            environ=environ,
        ),
    ):
        with pytest.raises(EvolveRefused) as info:
            call()
        assert str(info.value) == "reflector_env_missing at reflector.api_key_env"
    assert not (tmp_path / "run").exists()


def test_admit_output_root_returns_the_finding_when_its_digest_is_admitted(tmp_path: Path) -> None:
    from evolution.tree.execution.authorizations import hygiene_instance_digest
    from evolution.tree.execution.output_admission import HygieneFinding, admit_output_root

    root = tmp_path / "scorer-traces"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    with pytest.raises(EvolveRefused) as refusal:
        admit_output_root(root)
    assert refusal.value.code == "run_dir_unsafe_entry"
    assert refusal.value.path == "scorer-traces"
    expected = hygiene_instance_digest("scorer-traces", "not owner-only", "0o755")
    assert refusal.value.instance_digest == expected

    finding = admit_output_root(root, admitted={expected})
    assert finding == HygieneFinding("scorer-traces", "not owner-only", "0o755", expected)
    # A different fact at the same entry is a new refusal, not the admitted one.
    os.chmod(root, 0o775)
    with pytest.raises(EvolveRefused) as again:
        admit_output_root(root, admitted={expected})
    assert again.value.instance_digest != expected
    os.chmod(root, 0o700)
    assert admit_output_root(root, admitted={expected}) is None


def test_admit_output_root_symlink_fact_is_the_link_target(tmp_path: Path) -> None:
    from evolution.tree.execution.authorizations import hygiene_instance_digest
    from evolution.tree.execution.output_admission import admit_output_root

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    link = tmp_path / "editor-logs"
    link.symlink_to(elsewhere)
    with pytest.raises(EvolveRefused) as refusal:
        admit_output_root(link)
    assert refusal.value.instance_digest == hygiene_instance_digest(
        "editor-logs", "symlinked", str(elsewhere)
    )


def test_evolve_refused_carries_differences_and_digest_without_changing_str() -> None:
    from evolution.tree.execution.authorizations import RefusalDifference

    refusal = EvolveRefused(
        "run_identity_changed",
        differences=(RefusalDifference("producing_version", "0.3.0a11", "0.3.0a12"),),
        instance_digest="a" * 64,
    )
    assert str(refusal) == "run_identity_changed"
    assert refusal.differences[0].field == "producing_version"
    assert refusal.instance_digest == "a" * 64
    assert EvolveRefused("bad_value", "scorer.cwd").differences == ()
    assert EvolveRefused("bad_value", "scorer.cwd").instance_digest is None


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_process_logs_root_rejects_unsafe_types(tmp_path: Path, kind: str) -> None:
    from evolution.tree.evolve import run_dir_state

    run = tmp_path / "run"
    run.mkdir()
    logs = run / "logs"
    if kind == "symlink":
        target = tmp_path / "elsewhere"
        target.mkdir()
        logs.symlink_to(target, target_is_directory=True)
    else:
        logs.write_text("foreign")
    with pytest.raises(EvolveRefused) as caught:
        run_dir_state(run, roots=FAMILY14_OUTPUT_ROOTS)
    assert (caught.value.code, caught.value.path) == ("run_dir_unsafe_entry", "logs")
    if kind == "file":
        assert caught.value.instance_digest is None
    else:
        assert caught.value.instance_digest is not None


@pytest.mark.parametrize("family15", [False, True])
@pytest.mark.parametrize("declared", [False, True])
def test_b17_chat_prompt_selection_keeps_declared_and_v15_bytes(tmp_path, family15, declared):
    from evolution import prompts

    prompt = tmp_path / "custom.md"
    prompt.write_text("custom prompt\n")
    section = chat_section(system_prompt=prompt if declared else None)
    kwargs = dict(
        seed_root=tmp_path / "seed",
        run_dir=tmp_path / "run",
        environ={"E1_KEY": "key"},
        family15=family15,
    )
    editor = build_editor(section, **kwargs)
    reflector = build_reflector(
        ReflectorSection(role=section, environment_docs=None, probe=None),
        model_grouping_family=True,
        result_repair=family15,
        **kwargs,
    )
    assert editor.prompt.text() == (
        "custom prompt\n"
        if declared
        else getattr(prompts, "TREE_EDITOR_SYSTEM_V2" if family15 else "TREE_EDITOR_SYSTEM")
    )
    assert reflector.prompt.text() == (
        "custom prompt\n"
        if declared
        else getattr(
            prompts, "TREE_REFLECTOR_SYSTEM_V5" if family15 else "TREE_REFLECTOR_SYSTEM_V3"
        )
    )
