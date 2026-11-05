"""Output admission for tree execution."""

from __future__ import annotations

import os
import stat
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from evolution.tree.execution.authorizations import (
    Effect,
    RefusalDifference,
    hygiene_instance_digest,
)

OUTPUT_ROOTS: tuple[str, ...] = (
    "scorer-traces",
    "editor-logs",
    "reflector-logs",
    "environment-docs",
)


PROCESS_LOGS_DIR = "logs"


FAMILY14_OUTPUT_ROOTS: tuple[str, ...] = (*OUTPUT_ROOTS, "connection-logs", PROCESS_LOGS_DIR)


def output_roots(schema: str) -> tuple[str, ...]:
    """The output roots a run directory of this manifest schema owns."""

    if schema in ("evolution-v2-manifest/15", "evolution-v2-manifest/16"):
        return FAMILY14_OUTPUT_ROOTS
    return OUTPUT_ROOTS


@dataclass(frozen=True)
class RefusalFinding:
    """One finding of an admission: what `refused:`/`differs:`/`class:` print.

    A single-finding refusal is `EvolveRefused`'s own singular fields, echoed
    here as the one-element `findings` tuple every `EvolveRefused` carries. A
    collected refusal (every `run_dir_unsafe_entry` of the run directory,
    then the identity or lineage finding) is several of these, built through
    `EvolveRefused.from_findings`, so a caller that walks `findings` sees the
    same shape whichever way the refusal was raised.
    """

    code: str
    path: str | None
    detail: str
    differences: tuple[RefusalDifference, ...]
    instance_digest: str | None
    effect: Effect | None
    #: Every effect an accepted decision may choose, default first;
    #: `effect` names the default. Several only for a family-7
    #: comparable `run_identity_changed`; `(effect,)` for every other finding
    #: with an effect, empty for one without.
    effects: tuple[Effect, ...] = ()
    #: A `/14` policy refusal's fixed statement of the accepted domain (it never
    #: carries the refused value), or `None`; the CLI prints it after `refused:`.
    expected: str | None = None

    def __post_init__(self) -> None:
        if not self.effects and self.effect is not None:
            object.__setattr__(self, "effects", (self.effect,))


class EvolveRefused(ValueError):
    """`evo tree evolve` refuses to start, or to continue, this invocation.

    The public refusal is `code` and, where one exists, a `path`: a dotted
    manifest path (`scorer.cwd`) or the name of the run-directory entry at
    fault (`scorer-traces`). `str()` is `"<code>"` or `"<code> at <path>"` and
    carries no manifest value, filesystem path or environment value. `detail`
    is the private underlying message, which may name a resolved path; it is
    absent from `str()` and from `args`.

    `differences` lists what differs for `run_identity_changed`; it is
    empty for every other code. `instance_digest` is the digest an authorization
    must quote, `None` for a hard refusal. Both are public -- they name fields
    and carry digests, never values -- and neither touches `str()`, so every
    caller that prints or matches on a refusal reads exactly what it read before.

    `effect` is what an accepted decision would do (PDR-0012, §5.1, §5.4):
    `rebase` and `hygiene` for the two acceptances with no sentence of their
    own (a continuation under a rebased identity, an admitted directory
    entry), `EFFECT_SENTENCES`' own keys (`close`, `terminal`, `link`) for the
    three that print one, and `None` for a refusal with no instance digest,
    which nothing can accept. `findings` is every finding this refusal
    reports, in order; a caller that raises one finding at a time never
    passes it, and `__init__` fills it with the one finding the singular
    fields above already describe, so `findings` is never empty.

    `effects` lists every effect a decision may choose, default
    first; `effect` stays and names the default. A family-7 comparable
    `run_identity_changed` offers several (`continue`, `rebaseline`, `close`);
    a caller that only uses the default effect reads `effect` and still has the digest.
    """

    def __init__(
        self,
        code: str,
        path: str | None = None,
        detail: str = "",
        *,
        differences: tuple[RefusalDifference, ...] = (),
        instance_digest: str | None = None,
        effect: Effect | None = None,
        findings: tuple[RefusalFinding, ...] = (),
        effects: tuple[Effect, ...] = (),
        expected: str | None = None,
    ) -> None:
        super().__init__(code if path is None else f"{code} at {path}")
        self.expected = expected
        self.code = code
        self.path = path
        self.detail = detail
        self.differences = differences
        self.instance_digest = instance_digest
        self.effect = effect
        # A refusal with an effect offers at least that one.
        self.effects = effects or (() if effect is None else (effect,))
        self.findings = findings or (
            RefusalFinding(
                code, path, detail, differences, instance_digest, effect, effects, expected
            ),
        )

    @classmethod
    def from_findings(cls, findings: Sequence[RefusalFinding]) -> EvolveRefused:
        """Build one refusal whose singular fields are `findings[0]`.

        The CLI and every other reader that has not been taught about
        collected findings keeps reading `code`/`path`/`instance_digest` as
        the first -- and, before collection existed, the only -- finding; the
        full set is on `findings` for a reader that wants every one of them.
        """

        first = findings[0]
        return cls(
            first.code,
            first.path,
            first.detail,
            differences=first.differences,
            instance_digest=first.instance_digest,
            effect=first.effect,
            findings=tuple(findings),
            effects=first.effects,
            expected=first.expected,
        )


@dataclass(frozen=True)
class HygieneFinding:
    """One accepted-or-refused fact about a run-directory entry (PDR-0010, §5).

    `guard` is one of `authorizations.GUARD_CLASSES` and `fact` is the observed
    side of it -- the link target, the owning uid, the permission bits -- so the
    `instance_digest` is bound to *this* state of *this* entry and an
    authorization for one never silently covers another.
    """

    name: str
    guard: str
    fact: str
    instance_digest: str


def _finding(name: str, guard: str, fact: str) -> HygieneFinding:
    return HygieneFinding(name, guard, fact, hygiene_instance_digest(name, guard, fact))


def _refuse_or_admit(finding: HygieneFinding, admitted: Collection[str]) -> HygieneFinding:
    if finding.instance_digest in admitted:
        return finding
    raise EvolveRefused(
        "run_dir_unsafe_entry",
        finding.name,
        detail=finding.guard,
        instance_digest=finding.instance_digest,
        effect="hygiene",
    )


def admit_output_root(path: Path, *, admitted: Collection[str] = ()) -> HygieneFinding | None:
    """Admit one of the output roots, before any write reaches it.

    The entry is admissible when it is absent -- the binding creates it on its
    first use -- or when `lstat` shows a directory that is not a symlink, is
    owned by the effective user, and carries no group or other permission bit.
    Every component of the path above it must be a real directory too: a
    symlinked component would put the traces somewhere other than under the
    canonical run directory, which is what the admission rule must
    prevent. `evolve` canonicalizes the run directory before it gets here,
    so an ordinary symlinked ancestor -- `/tmp` on macOS, a
    symlinked `$HOME` -- is resolved rather than refused, and this walk keeps
    the case it was written for: a component swapped for a symlink between
    admission and a write, which is the point-of-use re-check's whole subject.
    S1's `assert_private_directory` is not the authority here -- it checks the
    entry's type and owner but not its permission bits.

    The refusal names the root (`scorer-traces`), never the path that failed:
    an absolute path must stay out of stderr and journals.

    An entry that is none of those things is not always a refusal. `None` comes
    back for a clean entry; the :class:`HygieneFinding` comes back when its
    instance digest is in `admitted` -- a decision the operator journaled or
    supplied (PDR-0010) -- and only an unadmitted finding is raised, as
    an `EvolveRefused` carrying the digest an authorization must quote. "Not a
    directory" stays hard whatever is admitted: a file where a directory must be
    is removed, not accepted.
    """

    found = _judge_output_root(path)
    return None if found is None else _refuse_or_admit(found, admitted)


def collect_output_root_findings(
    root: Path, admitted: Collection[str], *, roots: Sequence[str] = OUTPUT_ROOTS
) -> tuple[HygieneFinding, ...]:
    """Every unadmitted guard finding of the output roots, collected.

    Each of `roots` (`output_roots(schema)`; the four by default) is judged
    independently, exactly as
    `admit_output_root` judges it, and a finding whose instance digest is not in
    `admitted` is *returned* rather than raised, so the caller can report every
    authorizable entry of the run directory in one refusal. The hard cases stay
    hard and are raised as today: a regular file where a root must be ("not a
    directory") and an `OSError` from `lstat` -- neither is a finding a decision
    could accept.
    """

    found = (_judge_output_root(Path(root) / name) for name in roots)
    return tuple(
        finding
        for finding in found
        if finding is not None and finding.instance_digest not in admitted
    )


def _judge_output_root(path: Path) -> HygieneFinding | None:
    """One output root's guard finding, or `None` when it is clean; hard cases raise."""

    # `abspath`, not `realpath`: the entry need not exist yet, and `realpath`
    # would resolve away the very symlink the walk below is looking for. The
    # price is that `abspath` collapses `..` *lexically* -- `a/link/../b` becomes
    # `a/b` whatever `link` points at -- so a caller that hands this function a
    # path with a `..` in it is asking about a directory the kernel may not
    # agree with. `evolve` hands it a `realpath`-canonical root, which has none.
    target = Path(os.path.abspath(path))
    name = target.name
    for parent in reversed(target.parents):
        if parent.is_symlink():
            return _finding(name, "symlinked", os.readlink(parent))
    try:
        entry = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EvolveRefused("run_dir_unsafe_entry", name, detail=str(exc)) from exc
    # `lstat`, so the entry's own type is known before its mode is read, and a
    # symlinked root is named as one. The base refused it too -- `S_ISDIR`
    # follows nothing, so a link fell through to "not a directory" below -- but
    # that is the wrong diagnosis: the admission rule makes `symlinked` an
    # authorizable finding whose fact is the link target. Asking the type
    # question first is what lets the link be reported, and accepted, as the
    # link it is rather than as a file where a directory must be.
    if stat.S_ISLNK(entry.st_mode):
        return _finding(name, "symlinked", os.readlink(target))
    if not stat.S_ISDIR(entry.st_mode):
        raise EvolveRefused("run_dir_unsafe_entry", name, detail="not a directory")
    if entry.st_uid != os.geteuid():
        return _finding(name, "foreign owner", str(entry.st_uid))
    if stat.S_IMODE(entry.st_mode) & 0o077:
        return _finding(name, "not owner-only", oct(stat.S_IMODE(entry.st_mode)))
    return None


__all__ = [
    "EvolveRefused",
    "FAMILY14_OUTPUT_ROOTS",
    "HygieneFinding",
    "OUTPUT_ROOTS",
    "PROCESS_LOGS_DIR",
    "RefusalFinding",
    "admit_output_root",
    "collect_output_root_findings",
    "output_roots",
]
