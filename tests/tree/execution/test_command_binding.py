"""Behavioral contracts for the immutable bound program invocation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from evolution.tree.execution.command_binding import CommandBinding, CommandChanged, argv_operands


def make_program(root: Path, name: str = "prog", body: str = "print('ok')\n") -> Path:
    cwd = root / name
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "main.py").write_text(body, encoding="utf-8")
    return cwd


def test_binding_resolves_executable_and_retains_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound invocation fixes the program and its environment once."""

    cwd = make_program(tmp_path)
    monkeypatch.setenv("W1_TOKEN", "secret")

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset({"W1_TOKEN"}),
    )

    assert binding.executable == Path(sys.executable).resolve()
    assert binding.child_env["W1_TOKEN"] == "secret"
    assert "PATH" in binding.child_env
    assert binding.spawn_argv()[0] == str(binding.spawn_path)
    assert binding.spawn_argv()[1:] == ("main.py",)
    assert cwd / "main.py" in binding.proof_paths


def test_binding_resolves_bare_name_through_child_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare program name is resolved once, against the child environment's PATH."""

    cwd = make_program(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    shim = fake_bin / "myinterp"
    shim.write_text("#!/bin/sh\nexec " + sys.executable + ' "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    binding = CommandBinding(
        argv=("myinterp", "main.py"), cwd=cwd, dependency_paths=(), allowed_env=frozenset()
    )

    assert binding.executable == shim.resolve()
    with pytest.raises(ValueError, match="executable not found"):
        CommandBinding(
            argv=("no-such-program-w1",), cwd=cwd, dependency_paths=(), allowed_env=frozenset()
        )


@pytest.mark.parametrize("bound_counterpart", [True, False], ids=["different-file", "missing-file"])
def test_a_relative_path_result_keeps_its_process_directory_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound_counterpart: bool
) -> None:
    """A completed PATH lookup cannot be reinterpreted under the command's cwd."""

    site = tmp_path / "site"
    cwd = make_program(tmp_path, "program")
    (site / "bin").mkdir(parents=True)
    actual = site / "bin" / "actual"
    actual.write_text("#!/bin/sh\nprintf 'site\\n'\n", encoding="utf-8")
    actual.chmod(0o755)
    runner = site / "bin" / "runner"
    runner.symlink_to("actual")
    if bound_counterpart:
        (cwd / "bin").mkdir()
        counterpart = cwd / "bin" / "runner"
        counterpart.write_text("#!/bin/sh\nprintf 'bound\\n'\n", encoding="utf-8")
        counterpart.chmod(0o755)
    monkeypatch.chdir(site)

    binding = CommandBinding(argv=("runner",), cwd=cwd, base_env={"PATH": "bin"})

    assert binding.spawn_path == runner
    assert binding.spawn_argv() == (str(runner),)
    assert binding.executable == actual
    assert binding.proof_paths == (actual,)
    # Pinned from d0722ae5f with this fixture; the real-file label is actual.
    assert binding.identity_digest() == (
        "7cf3701de0855087c53e16b4fd21cc17d15a238ec43ef7d2b88f3f2efbb3fa84"
    )
    result = subprocess.run(
        binding.spawn_argv(),
        cwd=binding.cwd,
        env=binding.child_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "site\n"
    binding.verify()


@pytest.mark.parametrize("where", ["script", "cwd", "dependency", "executable"])
def test_program_inside_seed_is_refused(tmp_path: Path, where: str) -> None:
    """A seed the run may edit can never also be the program that runs."""

    seed = tmp_path / "seed"
    (seed / "skills").mkdir(parents=True)
    outside = make_program(tmp_path, "outside")
    argv: tuple[str, ...]
    deps: tuple[Path, ...]
    if where == "script":
        (seed / "edit.py").write_text("", encoding="utf-8")
        argv, cwd, deps = (sys.executable, str(seed / "edit.py")), outside, ()
    elif where == "cwd":
        argv, cwd, deps = (sys.executable, "main.py"), make_program(seed, "prog"), ()
    elif where == "dependency":
        inside = seed / "data.txt"
        inside.write_text("x", encoding="utf-8")
        argv, cwd, deps = (sys.executable, "main.py"), outside, (inside,)
    else:
        exe = seed / "runner"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        argv, cwd, deps = (str(exe),), outside, ()

    with pytest.raises(ValueError, match="program_inside_seed"):
        CommandBinding(
            argv=argv, cwd=cwd, dependency_paths=deps, allowed_env=frozenset(), seed_root=seed
        )


def test_binding_outside_the_seed_is_accepted(tmp_path: Path) -> None:
    """Seed containment refuses only what the seed owns, not every declared program."""

    seed = tmp_path / "seed"
    (seed / "skills").mkdir(parents=True)
    cwd = make_program(tmp_path, "outside")
    dep = tmp_path / "dep.txt"
    dep.write_text("x", encoding="utf-8")

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        dependency_paths=(dep,),
        allowed_env=frozenset(),
        seed_root=seed,
    )

    assert binding.seed_root == seed.resolve()
    assert dep.resolve() in binding.proof_paths


def test_verify_detects_script_dependency_env_and_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sealed input is checked: program bytes, dependencies and the child environment."""

    cwd = make_program(tmp_path)
    dep = tmp_path / "dep.txt"
    dep.write_text("v1", encoding="utf-8")
    monkeypatch.setenv("W1_TOKEN", "a")
    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        dependency_paths=(dep,),
        allowed_env=frozenset({"W1_TOKEN"}),
    )

    binding.verify()
    (cwd / "main.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(CommandChanged):
        binding.verify()
    (cwd / "main.py").write_text("print('ok')\n", encoding="utf-8")
    binding.verify()
    dep.write_text("v2", encoding="utf-8")
    with pytest.raises(CommandChanged):
        binding.verify()
    dep.write_text("v1", encoding="utf-8")
    monkeypatch.setenv("W1_TOKEN", "b")
    with pytest.raises(CommandChanged):
        binding.verify()
    monkeypatch.setenv("W1_TOKEN", "a")
    monkeypatch.setenv("PATH", "/nonexistent:" + os.environ["PATH"])
    with pytest.raises(CommandChanged):
        binding.verify()


def test_a_rotated_environment_value_changes_the_seal_and_not_the_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDR-0003: the value is operator-trusted, so it is sealed but not journaled.

    Rotating a forwarded token must not make a resume `run_identity_changed` --
    the amended decision puts the declared programs' process environment outside
    the frozen configuration -- and no journaled record may carry a digest of a
    credential. The value is still sealed inside the run: `verify()` catches the
    rotation through the execution seal's `child_env`.
    """

    cwd = make_program(tmp_path)
    monkeypatch.setenv("W1_TOKEN", "alpha")
    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        allowed_env=frozenset({"W1_TOKEN"}),
    )
    before = binding.identity_digest(timeout_seconds=5.0)

    monkeypatch.setenv("W1_TOKEN", "beta")

    assert binding.identity_digest(timeout_seconds=5.0) == before
    assert binding.identity_proof()["environment"] == {"W1_TOKEN": {"present": True}}
    assert "alpha" not in json.dumps(binding.identity_proof())
    with pytest.raises(CommandChanged):
        binding.verify()
    monkeypatch.delenv("W1_TOKEN")
    assert binding.identity_digest(timeout_seconds=5.0) != before


def test_verify_reports_a_vanished_sealed_file_as_a_change(tmp_path: Path) -> None:
    """A deleted program or dependency is a command change, not a stray OSError."""

    cwd = make_program(tmp_path)
    dep = tmp_path / "dep.txt"
    dep.write_text("v1", encoding="utf-8")
    binding = CommandBinding(argv=(sys.executable, "main.py"), cwd=cwd, dependency_paths=(dep,))

    binding.verify()
    dep.unlink()
    with pytest.raises(CommandChanged, match="unreadable"):
        binding.verify()
    dep.write_text("v1", encoding="utf-8")
    binding.verify()
    (cwd / "main.py").unlink()
    with pytest.raises(CommandChanged, match="unreadable"):
        binding.verify()


def test_identity_digest_ignores_path_but_not_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity proof seals command semantics, not the host's search path."""

    cwd = make_program(tmp_path)
    binding = CommandBinding(
        argv=(sys.executable, "main.py"), cwd=cwd, dependency_paths=(), allowed_env=frozenset()
    )

    before = binding.identity_digest()
    monkeypatch.setenv("PATH", "/nonexistent:" + os.environ["PATH"])
    assert binding.identity_digest() == before
    (cwd / "main.py").write_text("print('changed')\n", encoding="utf-8")
    assert binding.identity_digest() != before


def test_identity_proof_shape_matches_review_base(tmp_path: Path) -> None:
    """The identity proof keeps the exact shape the review-base scorer sealed."""

    cwd = make_program(tmp_path)
    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset({"HOME"}),
    )

    proof = binding.identity_proof(timeout_seconds=5.0)

    assert set(proof) == {"argv", "dependencies", "environment", "timeout_seconds"}
    assert proof["timeout_seconds"] == 5.0
    assert set(binding.identity_proof()) == {"argv", "dependencies", "environment"}
    # The one deviation from the review-base shape, and it is deliberate:
    # amendment A13 dropped the value digest, keeping the name and its presence.
    assert set(proof["environment"]["HOME"]) == {"present"}
    assert set(proof["dependencies"][0]) == {"index", "path", "sha256"}
    seal = binding.execution_seal()
    assert set(seal) >= {"argv", "dependencies", "environment", "executable", "cwd", "child_env"}
    assert "timeout_seconds" not in seal
    assert seal["cwd"] == str(cwd.resolve())
    assert seal["executable"]["path"] == str(binding.executable)


def test_binding_rejects_malformed_declarations(tmp_path: Path) -> None:
    """The declaration rules the scorer enforced are the binding's rules now."""

    cwd = make_program(tmp_path)
    dep = cwd / "main.py"
    with pytest.raises(ValueError, match="argv must contain nonblank strings"):
        CommandBinding(argv=(), cwd=cwd)
    with pytest.raises(ValueError, match="argv must contain nonblank strings"):
        CommandBinding(argv=(sys.executable, "  "), cwd=cwd)
    with pytest.raises(ValueError, match="cwd is invalid"):
        CommandBinding(argv=(sys.executable,), cwd=tmp_path / "missing")
    with pytest.raises(ValueError, match="cwd must be a directory"):
        CommandBinding(argv=(sys.executable,), cwd=dep)
    with pytest.raises(ValueError, match="dependency is invalid"):
        CommandBinding(argv=(sys.executable,), cwd=cwd, dependency_paths=(cwd / "missing",))
    with pytest.raises(ValueError, match="dependencies must be files"):
        CommandBinding(argv=(sys.executable,), cwd=cwd, dependency_paths=(cwd,))
    with pytest.raises(ValueError, match="dependency_paths must be unique"):
        CommandBinding(argv=(sys.executable,), cwd=cwd, dependency_paths=(dep, dep))
    with pytest.raises(ValueError, match="module command is not supported"):
        CommandBinding(argv=(sys.executable, "-m", "unsealed"), cwd=cwd)
    with pytest.raises(ValueError, match="environment names must be nonblank"):
        CommandBinding(argv=(sys.executable,), cwd=cwd, allowed_env=frozenset({"A=B"}))


# -- `python -m`: refused, because the launched file is never the bound one ----


@pytest.mark.parametrize(
    "argv_tail",
    [
        ("-m", "w1_mod"),
        ("-mw1_mod",),
        ("-Sm", "w1_mod"),
        ("-X", "dev", "-m", "w1_mod"),
    ],
    ids=["plain", "joined", "clustered", "after-an-option"],
)
def test_a_module_command_is_refused(tmp_path: Path, argv_tail: tuple[str, ...]) -> None:
    """`-m` names no file, so no file bound here is provably the one executed.

    The interpreter resolves the name at launch out of the child's `sys.path`, so
    a `PYTHONPATH` entry that acquires `w1_mod.py` after binding is what runs
    while `verify()` re-digests whatever was sealed. PDR-0005 wants the bound
    program to be the executed program; the form cannot give that, so it is
    refused. Every spelling that runs a module is refused, including the ones the
    eye misses: `-mNAME` written together and the `m` packed into a cluster
    (`python -Sm pkg` does run `pkg`).
    """

    cwd = make_program(tmp_path, "outside")

    with pytest.raises(ValueError, match="module command is not supported; declare a script path"):
        CommandBinding(
            argv=(sys.executable, *argv_tail),
            cwd=cwd,
            dependency_paths=(),
            allowed_env=frozenset(),
        )


def test_a_module_command_through_a_symlinked_interpreter_is_refused(tmp_path: Path) -> None:
    """The interpreter is recognized by what `argv[0]` resolves to, not by its name.

    `/outside/py -> python` is a Python interpreter and will read `-m` as `-m`. A
    detector that read the declared name would wave it through, which is the
    refused form arriving under a different spelling.
    """

    cwd = make_program(tmp_path, "outside")
    shim = tmp_path / "bin" / "py"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(sys.executable)

    with pytest.raises(ValueError, match="module command is not supported"):
        CommandBinding(
            argv=(str(shim), "-m", "w1_mod"),
            cwd=cwd,
            dependency_paths=(),
            allowed_env=frozenset(),
        )


def test_a_symlinked_venv_interpreter_is_spawned_by_its_declared_path(tmp_path: Path) -> None:
    """Resolving a venv interpreter before spawn loses its environment."""

    env = tmp_path / "env"
    venv.create(env, with_pip=False, symlinks=True)
    cwd = make_program(tmp_path, body="import sys\nprint(sys.prefix)\n")
    (cwd / "main.py").rename(cwd / "script.py")
    interpreter = env / "bin" / "python"
    binding = CommandBinding(argv=(str(interpreter), "script.py"), cwd=cwd)

    assert binding.spawn_argv()[0] == str(interpreter)
    result = subprocess.run(
        binding.spawn_argv(),
        cwd=binding.cwd,
        env=binding.child_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(env)
    assert binding.executable == Path(os.path.realpath(interpreter))


@pytest.mark.parametrize("declared", ["inside", "linked_directory"])
def test_a_seed_venv_interpreter_linking_outside_is_refused(tmp_path: Path, declared: str) -> None:
    """The directory a symlinked interpreter is spawned from is part of the program.

    Python finds its environment next to the invoked path, so spawning
    `<seed>/.venv/bin/python` loads the seed's `site-packages` and runs its
    `.pth` files although the real file lies outside. Containment therefore
    judges the spawn directory as well; a link into that directory from
    outside the seed is judged by where it leads.
    """

    seed = tmp_path / "seed"
    seed.mkdir()
    env = seed / ".venv"
    venv.create(env, with_pip=False, symlinks=True)
    cwd = make_program(tmp_path, "outside")
    if declared == "inside":
        interpreter = env / "bin" / "python"
    else:
        (tmp_path / "bin").symlink_to(env / "bin", target_is_directory=True)
        interpreter = tmp_path / "bin" / "python"
    assert not Path(os.path.realpath(interpreter)).is_relative_to(seed.resolve())

    with pytest.raises(ValueError, match="program_inside_seed"):
        CommandBinding(argv=(str(interpreter), "main.py"), cwd=cwd, seed_root=seed)


def test_retargeting_a_symlinked_interpreter_after_bind_fails_verify(tmp_path: Path) -> None:
    """A still-present original target cannot hide a retargeted spawn path."""

    cwd = make_program(tmp_path)
    program_a = cwd / "program-a"
    program_b = cwd / "program-b"
    program_a.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program_b.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    link = cwd / "interpreter"
    link.symlink_to(program_a)
    binding = CommandBinding(argv=(str(link), "main.py"), cwd=cwd)

    link.unlink()
    link.symlink_to(program_b)

    with pytest.raises(CommandChanged):
        binding.verify()


def test_the_identity_digest_of_a_symlinked_interpreter_is_unchanged(tmp_path: Path) -> None:
    """Keeping the declared spawn path must preserve journaled identity labels."""

    cwd = make_program(tmp_path)
    program = cwd / "program-a"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    (cwd / "interpreter").symlink_to("program-a")
    binding = CommandBinding(argv=("./interpreter", "main.py"), cwd=cwd, base_env={})

    # Computed on d0722ae5f: the dependency label is program-a, not interpreter.
    assert binding.identity_digest() == (
        "98f325d3b7049e8da3072ba3e26900801e7be6e05be045dd6de37fe017a932f6"
    )


@pytest.mark.parametrize(
    ("argv_tail", "seals_the_script"),
    [
        (("main.py", "-m", "fast"), True),
        (("-W", "ignore", "main.py", "-m", "x"), True),
        (("-c", "print('ok')", "-m"), False),
    ],
    ids=["program-flag", "after-a-separated-option-value", "after-inline-code"],
)
def test_a_m_past_the_interpreters_options_is_the_programs_own(
    tmp_path: Path, argv_tail: tuple[str, ...], seals_the_script: bool
) -> None:
    """`-m` only means the module option inside the interpreter's option prefix.

    `python run.py -m fast` is a script program with a flag of its own, and the
    file it runs is the sealed script; `python -W ignore run.py -m x` is the same
    declaration behind an option whose operand is a separate token; and after
    `-c` the rest of the command line is inline code and its arguments. Refusing
    any of these would refuse a legitimate declaration in the name of a module
    command that is not there.
    """

    cwd = make_program(tmp_path, "outside")

    binding = CommandBinding(
        argv=(sys.executable, *argv_tail),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset(),
    )

    assert ((cwd / "main.py").resolve() in binding.proof_paths) is seals_the_script
    binding.verify()


def test_a_non_python_program_keeps_its_own_m_argument(tmp_path: Path) -> None:
    """The refusal is about the interpreter's `-m`, not about the two characters."""

    cwd = make_program(tmp_path, "outside")
    binding = CommandBinding(
        argv=("/bin/cat", "-m", "notes"),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset(),
    )

    assert binding.executable == Path(os.path.realpath("/bin/cat"))


def test_a_script_path_program_binds_seals_and_verifies(tmp_path: Path) -> None:
    """The supported form: the declared script is the sealed file and the run one."""

    cwd = make_program(tmp_path, "outside")
    script = cwd / "main.py"

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        dependency_paths=(),
        allowed_env=frozenset(),
    )

    assert script.resolve() in binding.proof_paths
    assert binding.spawn_argv() == (str(binding.spawn_path), "main.py")
    binding.verify()
    script.write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(CommandChanged):
        binding.verify()


def test_a_pythonpath_entry_under_the_seed_is_dropped_before_the_environment_is_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evolving tree does not reach the sealed program through `PYTHONPATH`."""

    seed = tmp_path / "seed"
    (seed / "lib").mkdir(parents=True)
    outside = tmp_path / "libs"
    outside.mkdir()
    inside_entry = str(seed / "lib")
    outside_entry = str(outside)
    monkeypatch.setenv("PYTHONPATH", inside_entry + os.pathsep + outside_entry)
    cwd = make_program(tmp_path, "outside")

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        seed_root=seed,
        excluded_roots=(seed,),
    )

    assert binding.child_env["PYTHONPATH"] == outside_entry
    assert binding.dropped_pythonpath == (inside_entry,)
    # The same environment still verifies: the filter is re-applied, not skipped.
    binding.verify()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("PYTHONPATH", inside_entry + os.pathsep + str(other))
    with pytest.raises(CommandChanged):
        binding.verify()


def test_a_pythonpath_of_only_excluded_entries_leaves_no_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `PYTHONPATH` is not the same thing as none, so the name goes."""

    seed = tmp_path / "seed"
    (seed / "lib").mkdir(parents=True)
    (seed / "pkg").mkdir()
    entries = (str(seed / "lib"), str(seed / "pkg"))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(entries))
    cwd = make_program(tmp_path, "outside")

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        seed_root=seed,
        excluded_roots=(seed,),
    )

    assert "PYTHONPATH" not in binding.child_env
    assert binding.dropped_pythonpath == entries
    binding.verify()


def test_a_pythonpath_entry_is_judged_by_realpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link under the seed that leads out is kept; one outside that leads in is dropped."""

    seed = tmp_path / "seed"
    seed.mkdir()
    outside = tmp_path / "libs"
    outside.mkdir()
    (seed / "escape").symlink_to(outside, target_is_directory=True)
    (seed / "real").mkdir()
    (tmp_path / "sneak").symlink_to(seed / "real", target_is_directory=True)
    kept = str(seed / "escape")
    dropped = str(tmp_path / "sneak")
    monkeypatch.setenv("PYTHONPATH", kept + os.pathsep + dropped)
    cwd = make_program(tmp_path, "outside")

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        seed_root=seed,
        excluded_roots=(seed,),
    )

    assert binding.child_env["PYTHONPATH"] == kept
    assert binding.dropped_pythonpath == (dropped,)


def test_a_binding_without_excluded_roots_keeps_the_operators_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is the excluded roots' doing; a binding with none forwards as before."""

    libs = tmp_path / "libs"
    libs.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(libs))
    cwd = make_program(tmp_path, "outside")

    binding = CommandBinding(argv=(sys.executable, "main.py"), cwd=cwd)

    assert binding.child_env["PYTHONPATH"] == str(libs)
    assert binding.dropped_pythonpath == ()


# -- an explicit base environment: the one mapping the binding reads ----------


def test_an_explicit_environment_is_the_only_environment_the_binding_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `base_env`, the child environment and its presence facts come from it.

    A name the process environment has and the mapping lacks is absent -- it is
    not filled in from `os.environ` -- and a name both have carries the
    mapping's value. The identity proof's presence is the mapping's fact too, so
    a preparation that binds under an explicit environment proves the identity a
    run started under that environment proves.
    """

    cwd = make_program(tmp_path)
    monkeypatch.setenv("W1_TOKEN", "ambient-value")
    monkeypatch.setenv("W1_AMBIENT_ONLY", "ambient-only")

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        allowed_env=frozenset({"W1_TOKEN", "W1_AMBIENT_ONLY"}),
        base_env={"W1_TOKEN": "explicit-value"},
    )

    assert binding.child_env == {"W1_TOKEN": "explicit-value"}
    assert binding.identity_proof()["environment"] == {
        "W1_AMBIENT_ONLY": {"present": False},
        "W1_TOKEN": {"present": True},
    }


def test_an_explicit_environment_binding_verifies_against_its_own_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`verify()` re-derives the environment from the binding's copy, never the process's.

    The caller's mapping is copied at bind, so mutating it afterwards changes
    nothing, and a changed process environment is not this binding's
    environment at all. The sealed files are still checked.
    """

    cwd = make_program(tmp_path)
    base = {"W1_TOKEN": "explicit-value"}
    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        allowed_env=frozenset({"W1_TOKEN"}),
        base_env=base,
    )

    base["W1_TOKEN"] = "mutated-by-caller"
    monkeypatch.setenv("W1_TOKEN", "ambient-value")
    binding.verify()
    assert binding.child_env == {"W1_TOKEN": "explicit-value"}
    (cwd / "main.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(CommandChanged):
        binding.verify()


def make_shim(directory: Path, name: str, marker: str) -> Path:
    """An executable named `name` in `directory`, distinct from any other by `marker`."""

    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / name
    shim.write_text(f'#!/bin/sh\n# {marker}\nexec {sys.executable} "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return shim


def test_an_explicit_path_resolves_and_seals_the_bare_program_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare program name is searched on the mapping's `PATH`, for both proofs.

    The executable the child is spawned with and the file the identity proof
    seals for `argv[0]` are one file, found on the explicit `PATH`; a same-named
    program on the process's own `PATH` is neither.
    """

    cwd = make_program(tmp_path)
    ambient = make_shim(tmp_path / "ambient-bin", "w1interp", "ambient")
    explicit = make_shim(tmp_path / "explicit-bin", "w1interp", "explicit")
    monkeypatch.setenv("PATH", str(ambient.parent) + os.pathsep + os.environ["PATH"])

    binding = CommandBinding(
        argv=("w1interp", "main.py"),
        cwd=cwd,
        base_env={"PATH": str(explicit.parent)},
    )

    assert binding.executable == explicit.resolve()
    assert explicit.resolve() in binding.proof_paths
    assert ambient.resolve() not in binding.proof_paths


def test_an_explicit_environment_without_path_searches_the_system_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `PATH` in the mapping means the system's default search path, not the process's.

    `shutil.which` with no path falls back to `os.environ["PATH"]`, which is the
    hidden read an explicit environment rules out. The default is the one
    `shutil.which` itself takes when no `PATH` is set at all -- `CS_PATH` -- so a
    preparation under this mapping and a run started under it resolve alike.
    """

    cwd = make_program(tmp_path)
    ambient = make_shim(tmp_path / "ambient-bin", "w1interp", "ambient")
    monkeypatch.setenv("PATH", str(ambient.parent))

    with pytest.raises(ValueError, match="executable not found"):
        CommandBinding(argv=("w1interp", "main.py"), cwd=cwd, base_env={})
    binding = CommandBinding(argv=("sh", "-c", "true"), cwd=cwd, base_env={})
    default = shutil.which("sh", path=os.confstr("CS_PATH"))
    assert default is not None
    assert binding.executable == Path(default).resolve()


def test_the_pythonpath_guard_filters_an_explicit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment filter is the binding's own, whichever mapping it reads."""

    seed = tmp_path / "seed"
    (seed / "lib").mkdir(parents=True)
    outside = tmp_path / "libs"
    outside.mkdir()
    inside_entry = str(seed / "lib")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "ambient-libs"))
    cwd = make_program(tmp_path, "outside")

    binding = CommandBinding(
        argv=(sys.executable, "main.py"),
        cwd=cwd,
        seed_root=seed,
        base_env={"PYTHONPATH": inside_entry + os.pathsep + str(outside)},
    )

    assert binding.child_env == {"PYTHONPATH": str(outside)}
    assert binding.dropped_pythonpath == (inside_entry,)
    binding.verify()


def test_an_explicit_environment_must_map_strings_to_strings(tmp_path: Path) -> None:
    """A mapping no child could be spawned with is refused at bind, not at spawn."""

    cwd = make_program(tmp_path)

    with pytest.raises(ValueError, match="base environment must map strings to strings"):
        CommandBinding(argv=(sys.executable, "main.py"), cwd=cwd, base_env={"W1_TOKEN": 1})


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("python3", "score.py"), ((1, "score.py"),)),
        (
            ("python3", "-u", "score.py", "--flag", "data/in.json"),
            ((2, "score.py"), (4, "data/in.json")),
        ),
        (("python3", "-c", "print(1)", "after.txt"), ((3, "after.txt"),)),
        (("python3", "-m", "pkg.mod", "after.txt"), ((3, "after.txt"),)),
        (("tool",), ()),
        (("tool", "-c"), ()),
        (("tool", "--", "x"), ((2, "x"),)),
    ],
)
def test_argv_operands_skips_flags_and_the_c_m_operand(
    argv: tuple[str, ...], expected: tuple[tuple[int, str], ...]
) -> None:
    """The one lexical walk admission, binding and the inventory share."""
    assert argv_operands(argv) == expected
