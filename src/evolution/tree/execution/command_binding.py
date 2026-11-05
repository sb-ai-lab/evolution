"""One immutable binding of a declared program: what will run, from where,
with which environment, and the two proofs over it.

The *identity proof* is what a journaled identity hashes: the argv text, the
sealed program bytes, and the declared environment *names* with whether each is
present -- never a value or a digest of one, because PDR-0003 places the
programs' process environment outside the frozen configuration, so rotating a
forwarded token must not turn a resume into ``run_identity_changed`` and no
journal may carry a hash of a credential. The *execution seal* adds the absolute
executable, ``cwd`` and the complete retained child environment, values hashed;
``verify()`` checks it, which is where a changed value is caught, and it is
never journaled. Spawning consumes ``spawn_argv()`` and ``child_env``, never the
textual ``argv`` or a fresh environment read.

The environment a binding reads is ``base_env`` when the caller gives one: a
copy taken at bind, and from then on the only environment read. The child
environment, each declared name's presence, the search for a bare program name
and ``verify()`` all come from it, so a later change to the process environment
reaches none of them, and ``verify()`` checks the sealed files alone. Without
``base_env`` the process environment is read live, as it always was, and
``verify()`` is also where a value changed under a running campaign is caught.

What the binding seals is what the manifest names or resolves from it. What a
declared program *loads* once it is running -- a module off ``PYTHONPATH``, a
shell it spawns, a launcher it re-executes -- is the program's own
responsibility under the operator's trust, not a property any binding can
prove (PDR-0005, amended 2026-09-08). Two
best-effort guards close the common misconfigurations without claiming more.

The first is a refusal. A ``python -m`` program is not supported in this
preview and is refused at bind. ``-m NAME`` is resolved by the interpreter at
launch, out of whatever ``sys.path`` the child process turns out to have, so no
file sealed here can be proved to be the one executed: a ``PYTHONPATH`` entry
that acquires ``NAME.py`` after binding wins, while ``verify()`` re-digests the
file that was sealed rather than the file that will run. PDR-0005 asks for a
bound program, so the form is refused instead of approximated; an operator with
a module program declares the script path, and a two-line wrapper script is
enough.

The second is this module's ``PYTHONPATH`` filter. ``minimal_child_env``
forwards the operator's ``PYTHONPATH``, and an entry of it under the evolving
seed tree or under the run directory would put importable code the campaign
itself rewrites ahead of the sealed program's own imports. Every such entry is
dropped from ``child_env`` before the environment is retained and sealed, and
the dropped text is kept on :attr:`CommandBinding.dropped_pythonpath` so a
caller can say that it happened. It is a guard, not a guarantee: an operator
program that computes its own ``sys.path`` is past it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from evolution.eval.child_env import minimal_child_env
from evolution.tree.execution.journal_identity import canonical_json


class CommandChanged(RuntimeError):
    """The sealed inputs of a bound command differ from the current ones."""


@dataclass(frozen=True)
class CommandBinding:
    """A declared program bound once to concrete bytes, paths and environment."""

    argv: tuple[str, ...]
    cwd: Path
    dependency_paths: tuple[Path, ...] = ()
    allowed_env: frozenset[str] = frozenset()
    seed_root: Path | None = None
    excluded_roots: tuple[Path, ...] = ()
    base_env: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    spawn_path: Path = field(init=False, repr=False)
    executable: Path = field(init=False, repr=False)
    child_env: Mapping[str, str] = field(init=False, repr=False, compare=False)
    proof_paths: tuple[Path, ...] = field(init=False, repr=False)
    dropped_pythonpath: tuple[str, ...] = field(init=False, repr=False, compare=False)
    _sealed: Mapping[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or any(not isinstance(part, str) or not part.strip() for part in argv):
            raise ValueError("argv must contain nonblank strings")
        try:
            cwd = Path(self.cwd).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"cwd is invalid: {exc}") from exc
        if not cwd.is_dir():
            raise ValueError("cwd must be a directory")
        dependencies: list[Path] = []
        for raw_path in self.dependency_paths:
            try:
                path = Path(raw_path).resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"dependency is invalid: {exc}") from exc
            if not path.is_file():
                raise ValueError("dependencies must be files")
            dependencies.append(path)
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("dependency_paths must be unique")
        allowed_env = frozenset(self.allowed_env)
        if any(
            not isinstance(name, str) or not name.strip() or "=" in name for name in allowed_env
        ):
            raise ValueError("allowed environment names must be nonblank")
        if self.base_env is not None:
            if any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in self.base_env.items()
            ):
                raise ValueError("base environment must map strings to strings")
            # A copy, so the caller's mapping can change after bind and this
            # binding's environment cannot.
            object.__setattr__(self, "base_env", MappingProxyType(dict(self.base_env)))
        seed = None if self.seed_root is None else Path(os.path.realpath(self.seed_root))
        # The seed tree, when there is one, is always an excluded root: it is the
        # tree the campaign rewrites, and importable code inside it must not
        # reach the sealed program through the environment.
        excluded_roots = tuple(
            dict.fromkeys(
                (
                    *(() if seed is None else (seed,)),
                    *(Path(os.path.realpath(root)) for root in self.excluded_roots),
                )
            )
        )
        child_env, dropped = _drop_contained_pythonpath(
            minimal_child_env(self._environment(), allow=allowed_env), cwd, excluded_roots
        )
        search_path = _search_path(child_env)
        spawn_path, executable = _resolve_executable(argv[0], cwd, search_path)
        if is_python_module_command(argv, executable):
            raise ValueError(_MODULE_UNSUPPORTED)
        proof_paths = tuple(
            dict.fromkeys((*command_paths(argv, cwd, search_path=search_path), *dependencies))
        )
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "dependency_paths", tuple(dependencies))
        object.__setattr__(self, "allowed_env", allowed_env)
        object.__setattr__(self, "excluded_roots", excluded_roots)
        object.__setattr__(self, "spawn_path", spawn_path)
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "child_env", child_env)
        object.__setattr__(self, "proof_paths", proof_paths)
        object.__setattr__(self, "dropped_pythonpath", dropped)
        if seed is not None:
            object.__setattr__(self, "seed_root", seed)
            # The spawn directory counts too: Python finds its environment next
            # to the invoked path, so a seed venv whose interpreter links outside
            # would still load the seed's site-packages and run its .pth files.
            for candidate in (cwd, executable, spawn_path.parent, *proof_paths):
                real = Path(os.path.realpath(candidate))
                if real == seed or seed in real.parents:
                    raise ValueError("program_inside_seed")
        object.__setattr__(self, "_sealed", self.execution_seal())

    def identity_proof(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """The journaled proof: argv text, sealed bytes and the environment names.

        ``environment`` carries each declared name and whether it was present --
        an absent name and an empty one are different command semantics, and
        presence is a fact about the declaration, not a value. The value itself
        is deliberately absent: PDR-0003 makes the declared programs' process
        environment operator-trusted and outside the frozen configuration, so a
        rotated token is not a new campaign, and a journaled record must not
        carry a digest of a credential. Values are sealed in
        :meth:`execution_seal` and checked by :meth:`verify` within the run.
        """

        source = self._environment()
        environment = {name: {"present": name in source} for name in sorted(self.allowed_env)}
        dependencies = []
        for index, path in enumerate(self.proof_paths):
            try:
                label = str(path.relative_to(self.cwd))
            except ValueError:
                label = path.name
            dependencies.append({"index": index, "path": label, "sha256": file_digest(path)})
        proof: dict[str, Any] = {
            "argv": list(self.argv),
            "dependencies": dependencies,
            "environment": environment,
        }
        if timeout_seconds is not None:
            proof["timeout_seconds"] = float(timeout_seconds)
        return proof

    def identity_digest(self, *, timeout_seconds: float | None = None) -> str:
        """The digest a journaled identity carries for this bound command."""

        return _json_digest(self.identity_proof(timeout_seconds=timeout_seconds))

    def execution_seal(self) -> dict[str, Any]:
        """The run-time proof: the identity proof plus what will actually run.

        ``child_env`` hashes every retained value, so a value that changes under
        a running campaign is a ``CommandChanged`` here even though the identity
        proof never saw it. This seal stays in process and is never journaled.
        """

        seal = self.identity_proof()
        seal["executable"] = {
            "path": str(self.executable),
            "sha256": file_digest(self.executable),
        }
        seal["spawn"] = {
            "path": str(self.spawn_path),
            "realpath": os.path.realpath(self.spawn_path),
            "sha256": file_digest(self.spawn_path),
        }
        seal["cwd"] = str(self.cwd)
        seal["child_env"] = {
            name: hashlib.sha256(value.encode()).hexdigest()
            for name, value in sorted(self.child_env.items())
        }
        return seal

    def verify(self) -> None:
        """Raise ``CommandChanged`` when any sealed input differs from the current one.

        The current environment is read through the same ``PYTHONPATH`` filter
        the binding sealed, not raw: an entry the guard dropped at bind is
        dropped again here, so an unchanged environment still verifies and only
        a real difference -- a changed value, a new outside entry -- is caught.
        """

        try:
            current_seal = self.execution_seal()
        except OSError as exc:
            # A sealed file that vanished or turned unreadable is the largest
            # difference there is, never a stray OS error for a caller to unpack.
            raise CommandChanged("bound command inputs unreadable") from exc
        current_env, _ = _drop_contained_pythonpath(
            minimal_child_env(self._environment(), allow=self.allowed_env),
            self.cwd,
            self.excluded_roots,
        )
        if current_seal != dict(self._sealed) or current_env != dict(self.child_env):
            raise CommandChanged("bound command inputs changed")

    def _environment(self) -> Mapping[str, str]:
        """The one environment this binding reads: its own copy, or the process's.

        With ``base_env`` every read -- the child environment, the presence of a
        declared name, the re-derivation in :meth:`verify` -- is of the copy
        taken at bind, so nothing the process environment does afterwards
        reaches this binding. Without it the process environment is read live,
        as it always was.
        """

        return os.environ if self.base_env is None else self.base_env

    def spawn_argv(self) -> tuple[str, ...]:
        """The argv to spawn: the declared absolute path and the declared arguments."""

        return (str(self.spawn_path), *self.argv[1:])


def _drop_contained_pythonpath(
    child_env: Mapping[str, str], cwd: Path, excluded_roots: tuple[Path, ...]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Drop the ``PYTHONPATH`` entries that lie under an excluded root.

    ``minimal_child_env`` forwards the operator's ``PYTHONPATH``, and an entry
    of it under the seed tree or under the run directory is importable code the
    campaign itself rewrites, placed ahead of the sealed program's own imports.
    The entry is dropped rather than the whole variable, so an operator's
    unrelated library path survives; when nothing survives, the variable itself
    is removed rather than left empty, because an empty ``PYTHONPATH`` is not
    the same thing as none.

    Containment is judged by ``realpath``, so a link under the seed that points
    outside is kept and a link outside that points into the seed is dropped --
    the same rule the program-containment check applies. An empty entry means
    the child's working directory, which is ``cwd`` and not this process's own,
    so it is judged as ``cwd``; ``cwd`` under an excluded root is already
    ``program_inside_seed``, so in practice an empty entry is kept.

    Returns the filtered environment and the dropped entries as the operator
    wrote them. Those values are environment values: a caller may announce that
    a drop happened, but PDR-0003 and amendment A13 keep them out of every
    journaled identity, which records names and presence only.
    """

    env = dict(child_env)
    raw = env.get("PYTHONPATH")
    if raw is None or not excluded_roots:
        return env, ()
    kept: list[str] = []
    dropped: list[str] = []
    for entry in raw.split(os.pathsep):
        target = cwd if not entry else Path(os.path.realpath(entry))
        if any(target == root or root in target.parents for root in excluded_roots):
            dropped.append(entry)
        else:
            kept.append(entry)
    if not dropped:
        return env, ()
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        del env["PYTHONPATH"]
    return env, tuple(dropped)


def _search_path(child_env: Mapping[str, str]) -> str:
    """The search path a bare program name is resolved on: the child's own ``PATH``.

    A child environment without ``PATH`` is searched on the default
    ``shutil.which`` itself takes when no ``PATH`` is set -- ``CS_PATH``, or
    ``os.defpath`` where the system has none -- computed here rather than left
    to ``shutil.which(path=None)``, which would read this process's ``PATH``
    first. The default is a property of the system, not of any environment, so
    a binding under an explicit mapping resolves exactly as a process started
    under that mapping does, and a binding of the process environment resolves
    as it always did.
    """

    path = child_env.get("PATH")
    if path is not None:
        return path
    try:
        default = os.confstr("CS_PATH")
    except (AttributeError, ValueError):
        return os.defpath
    # `shutil.which` treats a missing default like an empty `PATH`: nothing is found.
    return default or ""


def _resolve_executable(name: str, cwd: Path, search_path: str) -> tuple[Path, Path]:
    if "/" in name:
        spawn_path = cwd / name
        try:
            executable = spawn_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"executable not found: {exc}") from exc
    else:
        found = shutil.which(name, path=search_path)
        if found is None:
            raise ValueError("executable not found")
        spawn_path = Path(found)
        if not spawn_path.is_absolute():
            spawn_path = Path.cwd() / spawn_path
        executable = spawn_path.resolve()
    if not executable.is_file():
        raise ValueError("executable not found")
    return spawn_path, executable


def argv_operands(argv: Sequence[str]) -> tuple[tuple[int, str], ...]:
    """Every ``argv[1:]`` entry that may name a file, with its index.

    The one lexical walk that admission (``manifest_v2._argv_program_paths``),
    binding (:func:`command_paths`) and the manifest inventory share, so the
    three cannot drift: an argument starting with ``-`` is a flag and is
    skipped; ``-c`` and ``-m`` also skip the operand that follows them (inline
    code and a module name are not files). Nothing here touches the filesystem;
    the callers decide whether an operand exists.
    """

    found: list[tuple[int, str]] = []
    skip_next = False
    for index in range(1, len(argv)):
        if skip_next:
            skip_next = False
            continue
        argument = argv[index]
        if argument in {"-c", "-m"}:
            skip_next = True
            continue
        if argument.startswith("-"):
            continue
        found.append((index, argument))
    return tuple(found)


def command_paths(
    argv: tuple[str, ...], cwd: Path, *, search_path: str | None = None
) -> tuple[Path, ...]:
    """Every existing file the declared command names: program, scripts, arguments.

    A bare ``argv[0]`` that is not a file under ``cwd`` is looked up on
    ``search_path``. :class:`CommandBinding` passes the search path it resolved
    the executable on, so the file sealed for ``argv[0]`` is the one spawned;
    ``None`` is ``shutil.which``'s own default, this process's ``PATH``.

    The operand of ``-c`` is skipped rather than read as a path, and that is
    sound: ``-c CODE`` runs inline code, and the code is ``argv`` text, which the
    identity proof already carries in full -- there is no unsealed file behind
    it, so ``-c`` needs no refusal of its own. ``-m`` is listed beside it only
    for symmetry; a module command never reaches here, because it is refused at
    bind.
    """

    paths: list[Path] = []
    executable = Path(argv[0])
    if not executable.is_absolute():
        local_executable = cwd / executable
        if local_executable.is_file():
            executable = local_executable
        else:
            resolved = shutil.which(argv[0], path=search_path)
            if resolved is not None:
                executable = Path(resolved)
    if executable.is_file():
        paths.append(executable.resolve())
    for _index, argument in argv_operands(argv):
        path = Path(argument)
        if not path.is_absolute():
            path = cwd / path
        try:
            if path.is_file():
                paths.append(path.resolve())
        except OSError:
            continue
    return tuple(dict.fromkeys(paths))


#: The refusal for a program the interpreter would launch as a module.
#:
#: ``-m NAME`` names no file. The interpreter resolves the name at launch, from
#: the ``sys.path`` the child process happens to have, so nothing sealed at bind
#: is provably what runs: an earlier ``PYTHONPATH`` entry that acquires
#: ``NAME.py`` after binding wins the resolution, and :meth:`CommandBinding.verify`
#: re-digests the file that was sealed, not the file that will be executed.
#: PDR-0005 requires a bound program, and the form cannot deliver one, so this
#: preview refuses it. An operator with a module program declares the script
#: path instead; a two-line wrapper script is enough, and the executed file is
#: then the sealed script.
_MODULE_UNSUPPORTED = "module command is not supported; declare a script path"


def _carries_module_option(token: str) -> bool:
    """Whether one ``argv`` token is, or hides, the interpreter's ``-m`` option.

    Three spellings run a module: ``-m`` with the name as the next word,
    ``-mNAME`` written together, and a short-option cluster ending in ``m``
    (``python -Sm pkg`` runs ``pkg``). All three are recognized so that all three
    are refused: a module command this predicate does not see is a program
    launched with no file sealed at all, which is the very hole PDR-0005 closes.
    """

    if not token.startswith("-") or token == "-":
        return False
    if token.startswith("--"):
        return False
    return "m" in token[1:]


#: The interpreter options that take their operand as a separate token when it
#: is not attached: `-X dev`, `-W ignore`, `--check-hash-based-pycs always`. The
#: operand is skipped, so a value of `run.py` is not mistaken for a script path
#: and a value of `-m` is not mistaken for the module option.
_SEPARATED_OPERAND_LONG = "--check-hash-based-pycs"
_SEPARATED_OPERAND_SHORT = frozenset("XW")


def is_python_module_command(argv: tuple[str, ...], executable: Path) -> bool:
    """Whether the interpreter would read a token of ``argv`` as its ``-m`` option.

    The interpreter is recognized by the basename of the *resolved* executable,
    never by ``argv[0]``'s text: ``/outside/py`` symlinked at a real interpreter
    is Python, and a check on the declared name alone would wave it through
    unseen -- which is exactly how the refused form would come back.

    Only the interpreter's own option prefix is scanned, because only there does
    ``-m`` mean the module option. ``python run.py -m fast`` is a program with a
    flag of its own and a sealed script; refusing it would refuse a legitimate
    declaration. The walk over ``argv[1:]`` therefore stops at the first token
    that ends the interpreter's options -- a token that does not start with
    ``-`` (the script path; everything after it is the program's), a ``-c``
    cluster (the rest is inline code), ``-`` (a script on standard input) and
    ``--`` -- and refuses only a ``-m`` reached before that point. An option that
    takes a separate operand (``-X dev``, ``-W ignore``,
    ``--check-hash-based-pycs always``) skips its operand, so neither a value
    that looks like a script nor one that looks like ``-m`` is misread.

    Within a short-option cluster the reading stays conservative: any cluster
    carrying an ``m`` is refused, ``-Sm`` and ``-Xm`` alike, though the second is
    really ``-X m``. A cluster is a rare spelling and the operator's fix is to
    write the options apart; the guarantee bought is that no module command binds
    unnoticed.
    """

    if not Path(os.path.realpath(executable)).name.lower().startswith("python"):
        return False
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if not token.startswith("-") or token in {"-", "--"}:
            # The script path, a script on standard input, or the end of the
            # interpreter's options: every token from here on is the program's.
            return False
        if _carries_module_option(token):
            return True
        if token == _SEPARATED_OPERAND_LONG:
            skip_next = True
            continue
        if token.startswith("--"):
            continue
        if "c" in token[1:]:
            # `-c`, or a cluster ending in it: the rest of the command line is
            # inline code and its arguments, not interpreter options.
            return False
        if token[-1] in _SEPARATED_OPERAND_SHORT:
            skip_next = True
    return False


def file_digest(path: Path) -> str:
    """The SHA-256 of a file read in bounded chunks."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
