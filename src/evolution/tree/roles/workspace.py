"""The private projection a Candidate editor edits, and the one path back.

An editor never lets an actor loose in the real Candidate. `project` admits the
Candidate root, pins its identity, captures it with a no-follow walk and copies
every regular file into a private temporary tree. The `Projection` keeps **two
baselines** of that tree: the *capture* is the Candidate as found, with the
modes it happens to carry, and the *baseline* is the projection as written, the
same bytes with every regular file at `0600`. Actor activity is diffed against
the baseline, never against the capture, so an untouched `0644` Candidate file
projected at `0600` is not a change while a file whose mode the actor altered
is. `admit` is the one authority on what that diff may contain; `publish` is
the only function in the module that writes the real Candidate, and it stages
from the capture plus the admitted changes -- never from a fresh copy of the
live workspace -- so nothing that appeared after `project` can be merged,
followed or published.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import IO, Any, Literal

import yaml

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.artifacts.artifact_policy import matches as policy_matches
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.private_files import (
    WorkspaceError,
    open_private_file,
)
from evolution.tree.storage.redaction import safe_record

MAX_NOTE_BYTES = 4096

PROJECTED_FILE_MODE = 0o600
"""Every projected regular file gets this one mode; see the two-baseline rule."""

MAX_CAPTURE_ENTRIES = 20_000
MAX_CAPTURE_BYTES = 256 * 1024 * 1024
"""The capture budget: a ceiling on what one walk will hold in memory."""

ROLE_PROJECTION_VERSION = "role_projection/1"

_ROOT = "."
_EXECUTABLE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
"""The artifact scan's executable rule (`artifact_policy.scan_tree`): any execute bit."""
_UNSAFE_FILE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o111
_UNSAFE_DIRECTORY_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022
_READ_CHUNK = 64 * 1024


class WorkspaceChanged(WorkspaceError):
    """The Candidate lost its anchor or drifted between capture and publication."""


class ControlCollision(WorkspaceError):
    """The Candidate already holds a top-level entry named like the control prefix."""


class ActorViolation(WorkspaceError):
    """Admission refused the actor's changes; `str()` is the redacted detail."""

    #: The closed `EditResult.code` an Editor kind returns for this refusal
    #: (ADR-0037): `actor_violation` for every refusal but the two budgets.
    code: str = "actor_violation"


class BudgetViolation(ActorViolation):
    """Admission refused on `max_changed_files` or `max_written_bytes` (ADR-0037 D6).

    `code` is `changed_file_budget` or `written_byte_budget`. `str()` keeps the
    detail every earlier release wrote, so a note and its digest read the same.
    """

    def __init__(
        self, code: Literal["changed_file_budget", "written_byte_budget"], detail: str
    ) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class WorkspaceAnchor:
    """The `(st_dev, st_ino)` identities that pin a workspace root in place."""

    root: tuple[int, int]
    parent: tuple[int, int]


@dataclass(frozen=True)
class Budgets:
    """The caps admission enforces over one actor's changes."""

    max_changed_files: int | None
    max_written_bytes: int
    max_payload_bytes: int


@dataclass(frozen=True)
class FileState:
    """One captured entry: a regular file's bytes and mode, or a directory's mode."""

    mode: int
    content: bytes
    kind: Literal["file", "dir"]


Capture = Mapping[str, FileState]
"""A relative POSIX path -> state map; the root itself is the key `"."`."""


@dataclass(frozen=True)
class Change:
    """One admitted file change, expressed against the projection's baseline.

    `mode` is the mode publication will give the entry, and only a `create`
    carries one: the mode the actor gave the file in the projection, which
    admission has already required to be a safe regular-file mode -- no setuid,
    setgid, sticky or execute bit. An `update` keeps the Candidate's own mode,
    which lives in the capture, and a `delete` has none.
    """

    path: str
    action: Literal["create", "update", "delete"]
    content: bytes | None
    mode: int | None


@dataclass
class Projection:
    """A private copy of one Candidate, with the two baselines and its control prefix."""

    workspace: Path
    root: Path
    control_prefix: str | None
    anchor: WorkspaceAnchor
    capture: Capture
    baseline: Capture
    _tmp: tempfile.TemporaryDirectory[str]
    control_snapshot: Capture = field(default_factory=dict)
    frozen: bool = False
    """Set by `freeze_projection`; `discard` restores directory bits whatever its value."""

    def changes(self) -> tuple[Change, ...]:
        """The projection as it stands now, diffed against the baseline."""

        current = _without_control(capture_tree(self.root), self.control_prefix)
        return _file_changes(self.baseline, current)

    def mark_control(self) -> None:
        """Re-snapshot the control subtree after the editor itself wrote to it.

        Admission compares the control subtree with this snapshot, so a file the
        editor writes under the control prefix is control-plane traffic rather
        than actor activity only once the editor has marked it.
        """

        self.control_snapshot = _control_subtree(capture_tree(self.root), self.control_prefix)


def matches_rule(relative: str, rule: str) -> bool:
    """Match a relative path with the same segment rules as ``Path.glob``."""

    parts = PurePosixPath(relative).parts
    pattern = PurePosixPath(rule).parts

    @cache
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern):
            return path_index == len(parts)
        segment = pattern[pattern_index]
        if segment == "**":
            return any(
                matches(index, pattern_index + 1) for index in range(path_index, len(parts) + 1)
            )
        return (
            path_index < len(parts)
            and fnmatch.fnmatchcase(parts[path_index], segment)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def relative_rule(rule: str) -> str:
    """Return a path *rule* that is relative, dot-free and whole-segment ``**``.

    This is the grammar of a **policy rule**, and of nothing else: a rule is a
    pattern the operator writes, so a leading dot in one of its segments can
    only be a traversal attempt and is refused wholesale. An actor-supplied
    *Candidate path* is a different grammar and has its own validator,
    `relative_candidate_path`, under which a dot-prefixed name such as
    ``.github`` is an ordinary file rather than a traversal.

    The segments are split from the raw string rather than from a
    ``PurePosixPath``, which silently drops a leading ``./`` and would let a dot
    segment through.
    """

    if not rule or "\0" in rule or rule.startswith("/"):
        raise ValueError(f"unsafe path rule: {rule!r}")
    for segment in rule.split("/"):
        if not segment or segment.startswith(".") or ("**" in segment and segment != "**"):
            raise ValueError(f"unsafe path rule: {rule!r}")
    return rule


def relative_candidate_path(value: str) -> str:
    """Return an actor-supplied Candidate path that is relative and traversal-free.

    The tools of an editor address files *inside the projection*, so what has
    to be refused here is an escape, not a naming convention: an absolute path,
    an empty path or empty segment (``a//b``, a trailing ``/``), a literal
    ``.`` or ``..`` segment, and NUL. A dot-prefixed name is not an escape --
    ``.github/workflows/ci.yml`` is a Candidate file the artifact policy may
    admit and `writable_paths` may mark writable (contract) -- so
    unlike `relative_rule`, which is the grammar of a policy *rule*, this
    validator lets it through. The segments are split from the raw string for
    the same reason `relative_rule` splits them: ``PurePosixPath`` would drop a
    leading ``./`` rather than refuse it.
    """

    if not value or "\0" in value or value.startswith("/"):
        raise ValueError(f"unsafe candidate path: {value!r}")
    for segment in value.split("/"):
        if not segment or segment in {os.curdir, os.pardir}:
            raise ValueError(f"unsafe candidate path: {value!r}")
    return PurePosixPath(value).as_posix()


def workspace_anchor(root: Path) -> WorkspaceAnchor:
    """Pin the root and its parent by `(st_dev, st_ino)`."""

    return WorkspaceAnchor(root=_directory_identity(root), parent=_directory_identity(root.parent))


def assert_anchored(root: Path, anchor: WorkspaceAnchor) -> None:
    """Refuse when the root or its parent is no longer the directory that was pinned."""

    if not _has_identity(root, anchor.root) or not _has_identity(root.parent, anchor.parent):
        raise WorkspaceChanged("Candidate workspace root changed")


def admit_workspace(root: Path) -> Path:
    """Admit a Candidate root: present, not a link, only directories and regular files."""

    try:
        details = root.lstat()
    except OSError as exc:
        raise WorkspaceError(f"Candidate workspace is unavailable: {root}") from exc
    if stat.S_ISLNK(details.st_mode):
        raise WorkspaceError(f"Candidate workspace must not be a symlink: {root}")
    if not stat.S_ISDIR(details.st_mode):
        raise WorkspaceError(f"Candidate workspace must be a directory: {root}")
    for entry in (root, *root.rglob("*")):
        mode = entry.lstat().st_mode
        if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
            raise WorkspaceError(f"Candidate workspace holds an unsupported entry: {entry}")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"Candidate workspace is unavailable: {root}") from exc


def capture_tree(root: Path) -> Capture:
    """Read a whole tree with `O_NOFOLLOW`, re-checking every entry's identity.

    Directories and files are opened by descriptor from their parent's
    descriptor, so no component of a path can be swapped for a symlink between
    the `scandir` entry and the read. Anything that is neither a directory nor a
    single-linked regular file is refused rather than followed.
    """

    try:
        root_status = root.lstat()
    except OSError as exc:
        raise WorkspaceError(f"Candidate workspace is unavailable: {root}") from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise WorkspaceError(f"Candidate workspace must be a regular directory: {root}")
    state: dict[str, FileState] = {_ROOT: FileState(stat.S_IMODE(root_status.st_mode), b"", "dir")}
    read_bytes = 0
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | close_on_exec
    file_flags = os.O_RDONLY | os.O_NONBLOCK | nofollow | close_on_exec

    def read_file(parent: int, name: str, relative: str, observed: os.stat_result) -> bytes:
        nonlocal read_bytes
        try:
            descriptor = os.open(name, file_flags, dir_fd=parent)
        except OSError as exc:
            raise WorkspaceError(f"cannot safely open Candidate file: {relative}") from exc
        try:
            details = os.fstat(descriptor)
            identity = _file_identity(details)
            if identity != _file_identity(observed) or not stat.S_ISREG(details.st_mode):
                raise WorkspaceError(f"Candidate file changed while inspecting: {relative}")
            if details.st_nlink != 1:
                raise WorkspaceError(f"Candidate holds a hard-linked file: {relative}")
            read_bytes += details.st_size
            if read_bytes > MAX_CAPTURE_BYTES:
                raise WorkspaceError("Candidate workspace exceeds the capture budget")
            remaining = details.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
                if not chunk:
                    raise WorkspaceError(f"Candidate file shrank while reading: {relative}")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1) or _file_identity(os.fstat(descriptor)) != identity:
                raise WorkspaceError(f"Candidate file changed while reading: {relative}")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def visit(descriptor: int, prefix: str, observed: os.stat_result) -> None:
        identity = _file_identity(observed)
        if _file_identity(os.fstat(descriptor)) != identity:
            raise WorkspaceError("Candidate directory changed while opening")
        try:
            entries = os.scandir(descriptor)
        except OSError as exc:
            raise WorkspaceError("cannot inspect Candidate directory") from exc
        with entries:
            for item in entries:
                if len(state) >= MAX_CAPTURE_ENTRIES:
                    raise WorkspaceError("Candidate workspace exceeds the capture budget")
                relative = f"{prefix}/{item.name}" if prefix else item.name
                try:
                    details = item.stat(follow_symlinks=False)
                except OSError as exc:
                    raise WorkspaceError(f"cannot inspect Candidate entry: {relative}") from exc
                if stat.S_ISDIR(details.st_mode):
                    state[relative] = FileState(stat.S_IMODE(details.st_mode), b"", "dir")
                    try:
                        child = os.open(item.name, directory_flags, dir_fd=descriptor)
                    except OSError as exc:
                        raise WorkspaceError(
                            f"cannot safely open Candidate directory: {relative}"
                        ) from exc
                    try:
                        visit(child, relative, details)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(details.st_mode):
                    state[relative] = FileState(
                        stat.S_IMODE(details.st_mode),
                        read_file(descriptor, item.name, relative, details),
                        "file",
                    )
                else:
                    raise WorkspaceError(f"Candidate holds an unsupported entry: {relative}")
        if _file_identity(os.fstat(descriptor)) != identity:
            raise WorkspaceError("Candidate directory changed while reading")

    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as exc:
        raise WorkspaceError(f"cannot safely open Candidate workspace: {root}") from exc
    try:
        visit(root_descriptor, "", root_status)
    finally:
        os.close(root_descriptor)
    return state


def visible_capture(capture: Capture, visible: Callable[[str], bool]) -> dict[str, FileState]:
    """The part of a capture a role may see (family 14, F0 R64).

    The root, every regular file `visible` accepts, and only the directories
    that are ancestors of such a file, each entry exactly as captured. An empty
    directory, or one that leads only to hidden files, is left out, so a
    projection reveals neither hidden names nor hidden structure.
    """

    files = {
        relative: entry
        for relative, entry in capture.items()
        if relative != _ROOT and entry.kind == "file" and visible(relative)
    }
    ancestors: set[str] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent.parts:
            ancestors.add(parent.as_posix())
            parent = parent.parent
    filtered: dict[str, FileState] = {}
    if _ROOT in capture:
        filtered[_ROOT] = capture[_ROOT]
    for directory in ancestors:
        entry = capture.get(directory)
        if entry is None or entry.kind != "dir":
            raise WorkspaceError(f"capture lacks the directory of a visible file: {directory}")
        filtered[directory] = entry
    filtered.update(files)
    return {relative: filtered[relative] for relative in sorted(filtered)}


def role_projection_digest(capture: Capture, visible: Callable[[str], bool]) -> str:
    """The `role_projection/1` digest of what a role sees of `capture` (F0 R67).

    `sha256(canonical_json({"version": "role_projection/1", "entries": [...]}))`
    with entries sorted by path: `{"path", "kind": "file", "executable",
    "sha256"}` per visible file and `{"path", "kind": "directory"}` per
    ancestor directory; the root is not an entry. `executable` is the artifact
    scan's rule (any execute bit), never the raw mode, and directory modes are
    not covered: the read-only published Parent and its `restore_writable`
    Attempt copy differ in raw modes and must share one digest.
    """

    entries: list[dict[str, object]] = []
    for relative, entry in visible_capture(capture, visible).items():
        if relative == _ROOT:
            continue
        if entry.kind == "file":
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "executable": bool(entry.mode & _EXECUTABLE_BITS),
                    "sha256": hashlib.sha256(entry.content).hexdigest(),
                }
            )
        else:
            entries.append({"path": relative, "kind": "directory"})
    payload = {"version": ROLE_PROJECTION_VERSION, "entries": entries}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def project(
    workspace: Path,
    *,
    control_prefix: str | None = None,
    tmp_root: Path | None = None,
    visible: Callable[[str], bool] | None = None,
    expected_digest: str | None = None,
) -> Projection:
    """Admit, capture and privately copy one Candidate for an actor to edit.

    `tmp_root` places the private projection under a caller-chosen directory
    (`TemporaryDirectory(dir=tmp_root)`) instead of the platform default. The
    campaign passes none, so the Reflector's and the Editors' projections both
    use the default temporary directory (`TMPDIR`, else `/tmp`).

    `visible` (family 14, F0 R64) makes the projection sparse: only
    `visible_capture(capture, visible)` is materialized. The capture itself
    stays full, and so does the control-collision check, so `admit` diffs the
    actor's work against the sparse baseline and `publish` stages from the full
    capture, keeping every hidden byte and mode. `expected_digest` binds the
    projection to a `role_projection/1` digest computed earlier; a mismatch is
    `WorkspaceChanged` before anything is written. Without `visible` the
    projection is the whole capture, exactly as before role visibility.
    """

    if expected_digest is not None and visible is None:
        raise ValueError("expected_digest requires visible")
    root = admit_workspace(workspace)
    capture = capture_tree(root)
    if control_prefix is not None and control_prefix in capture:
        raise ControlCollision(f"Candidate already holds the control prefix: {control_prefix}")
    state: Capture = capture
    if visible is not None:
        state = visible_capture(capture, visible)
        if (
            expected_digest is not None
            and role_projection_digest(capture, visible) != expected_digest
        ):
            raise WorkspaceChanged("role projection changed")
    anchor = workspace_anchor(root)
    temporary = tempfile.TemporaryDirectory(prefix="evo-tree-editor-", dir=tmp_root)
    try:
        projected = Path(temporary.name) / "workspace"
        projected.mkdir(mode=0o700)
        _materialize(_normalized(state), projected)
        baseline = _without_control(capture_tree(projected), control_prefix)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.cleanup()
        raise
    return Projection(
        workspace=root,
        root=projected,
        control_prefix=control_prefix,
        anchor=anchor,
        capture=capture,
        baseline=baseline,
        _tmp=temporary,
    )


def freeze_projection(projection: Projection) -> None:
    """Every entry read-only: the Reflector reads the Parent and never writes it.

    Every regular file under `projection.root` goes to `0o400` and every
    directory to `0o500` -- except the subtree named by
    `projection.control_prefix`, when one is set, which stays exactly as it
    was: an `agent_cli` reflector still writes its own
    `REFLECTION.json` there. That subtree is *created* here when it does not
    exist, because this is the last moment at which it can be: a Parent is
    published read-only, so the projection's own root may carry `0o500` from
    the capture, and after the freeze nothing may write the root at all.
    `discard` restores each directory to `0o700` before it removes the
    directory's contents.
    """

    prefix = projection.control_prefix
    root = projection.root
    if prefix is not None and not (root / prefix).exists():
        os.chmod(root, 0o700)
        (root / prefix).mkdir(mode=0o700, parents=True)
        projection.mark_control()

    def relative(dirpath: str) -> str:
        rel = os.path.relpath(dirpath, root)
        return "" if rel == os.curdir else rel.replace(os.sep, "/")

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        here = relative(dirpath)
        dirnames[:] = [
            name for name in dirnames if not _is_control(f"{here}/{name}" if here else name, prefix)
        ]
        for name in filenames:
            os.chmod(Path(dirpath) / name, 0o400, follow_symlinks=False)

    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        here = relative(dirpath)
        if _is_control(here, prefix):
            continue
        os.chmod(dirpath, 0o500)

    projection.frozen = True


def admit(
    projection: Projection, *, writable_paths: Sequence[str], budgets: Budgets
) -> tuple[Change, ...]:
    """Refuse or return the actor's changes; the one authority for both editors.

    `writable_paths` are the operator's patterns, admitted by the artifact policy
    when the manifest was bound, so they are matched here with that policy's own
    matcher -- the same authority `seal` and `ArtifactPolicyV1.is_writable` use --
    and never with the actor path grammar of `relative_rule`, which would refuse
    rules the policy has already accepted (`skill**`, `.github/**`). The
    semantics are the policy's, and they are gitignore-like rather than anchored:
    a pattern with no `/` matches a basename at any depth, so `*.md` admits
    `skills/a.md`. A dotted path is not vetoed here either -- `.github/**` is a
    legitimate writable set -- because the control plane is protected by its own
    exact-prefix comparison above, not by a hidden-segment rule.

    Over a sparse projection (`project(..., visible=...)`) the baseline lacks
    the hidden entries, so creating a path the full capture already holds as a
    file, or as a directory where the actor made a file, is refused -- but
    only after every other check has passed, so the refusal never
    distinguishes a hidden path from an absent one (`_hidden_collision`). A
    whole projection's baseline holds every captured path, so the rule
    changes nothing there.

    The re-capture of the projection is part of admission, not infrastructure:
    the projection is the actor's own output, so an entry the no-follow walk
    refuses -- a symlink, a FIFO, a device node, a hard link, a file that
    changed while it was being read -- is the actor's doing and carries the
    class every refusal here carries. Only the relative path the walk names
    travels into the detail; a root-level failure names a temporary directory
    rather than a Candidate path, and that path is not reported.
    """

    try:
        current = capture_tree(projection.root)
    except WorkspaceError as exc:
        detail = str(exc).rpartition(": ")[2]
        if not detail or detail.startswith("/"):
            detail = "projection root"
        raise ActorViolation(f"unsupported entry: {detail}") from exc
    if _control_subtree(current, projection.control_prefix) != projection.control_snapshot:
        raise ActorViolation("control plane touched")
    live = _without_control(current, projection.control_prefix)
    baseline = projection.baseline
    rules = tuple(writable_paths)
    added_directories: list[str] = []
    hidden_collisions: list[str] = []
    for relative in sorted(set(baseline) | set(live)):
        prior, now = baseline.get(relative), live.get(relative)
        if prior == now:
            continue
        if relative == _ROOT:
            raise ActorViolation("workspace mode changed")
        if prior is not None and now is not None and prior.kind != now.kind:
            raise ActorViolation(f"entry kind changed: {relative}")
        hidden = projection.capture.get(relative) if prior is None else None
        if (
            hidden is not None
            and now is not None
            and (hidden.kind != now.kind or now.kind == "file")
        ):
            # Recorded, and raised only after every other check: see `_hidden_collision`.
            hidden_collisions.append(relative)
        if (prior is not None and prior.kind == "file") or (now is not None and now.kind == "file"):
            if not _writable(relative, rules):
                raise ActorViolation(f"outside writable set: {relative}")
            if prior is not None and now is not None and prior.mode != now.mode:
                raise ActorViolation(f"mode changed: {relative}")
            if prior is None and now is not None and now.mode & _UNSAFE_FILE_BITS:
                raise ActorViolation(f"unsafe mode: {relative}")
            continue
        if prior is not None and now is not None:
            raise ActorViolation(f"directory mode changed: {relative}")
        if prior is not None:
            raise ActorViolation(f"directory removed: {relative}")
        assert now is not None
        if now.mode & _UNSAFE_DIRECTORY_BITS:
            raise ActorViolation(f"unsafe directory mode: {relative}")
        added_directories.append(relative)

    changes = _file_changes(baseline, live)
    created = {change.path for change in changes if change.action == "create"}
    for directory in added_directories:
        if not any(path.startswith(f"{directory}/") for path in created):
            raise ActorViolation(f"unused directory: {directory}")

    written = 0
    for change in changes:
        content = change.content
        if content is None:
            continue
        if len(content) > budgets.max_payload_bytes:
            raise ActorViolation(f"payload limit exceeded: {change.path}")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise ActorViolation(f"not UTF-8: {change.path}") from None
        written += len(content)
    if written > budgets.max_written_bytes:
        raise BudgetViolation("written_byte_budget", "written-byte budget exceeded")
    if (
        budgets.max_changed_files is not None
        and len(_counted_paths(changes)) > budgets.max_changed_files
    ):
        raise BudgetViolation("changed_file_budget", "changed-file budget exceeded")
    if hidden_collisions:
        raise _hidden_collision(hidden_collisions[0], projection.capture, live)
    return changes


def changed_file_paths(projection: Projection) -> tuple[str, ...]:
    """The files that differ from the projection's baseline now, sorted (ADR-0037 D3a).

    Exactly the set `admit` counts against `max_changed_files`: a file restored
    to its baseline bytes and mode is not a change, directories never count,
    and the control subtree is ignored. The chat Editor's plan and edit guards
    and the agent-CLI repair message read it, so the loop and the seal cannot
    disagree.
    """

    return _counted_paths(projection.changes())


def _counted_paths(changes: Sequence[Change]) -> tuple[str, ...]:
    """One path per created, updated or deleted file: what `max_changed_files` counts."""

    return tuple(change.path for change in changes)


def _hidden_collision(relative: str, capture: Capture, live: Capture) -> ActorViolation:
    """The refusal of a create over an entry a sparse projection left out (family 14).

    A sparse baseline lacks the hidden entries, so an actor can create a path
    the full capture already holds. Over the other kind -- a directory over a
    hidden file, a file over a hidden empty directory -- publication would fail
    mid-stage or replace the hidden entry; over a hidden file -- reachable only
    when a caller's `visible` omits a writable rule -- it would overwrite it. A
    directory over a hidden directory merges and is not a collision. `admit`
    raises this last, after every other check, so a refusal never tells the
    actor whether a path it may not touch is hidden or absent: the collision is
    reported only for a change that would otherwise be admitted. A whole
    projection's baseline holds every captured path, so this never arises there.
    """

    if capture[relative].kind != live[relative].kind:
        return ActorViolation(f"entry kind changed: {relative}")
    return ActorViolation(f"hidden entry collision: {relative}")


def publish(admitted: Sequence[Change], projection: Projection) -> None:
    """Write the staged tree into the real Candidate, atomically or not at all.

    The stage is the projection's capture plus the admitted changes. Immediately
    before the swap the live Candidate is recaptured and compared with that
    capture, and the anchors are re-checked; any difference is `WorkspaceChanged`
    with nothing written. The window between that check and the rename is the
    residual TOCTOU the dormant editors also had -- the Attempt workspace is
    run-owned, so a concurrent writer is a misuse this reports, not a mode.
    """

    root = projection.workspace
    assert_anchored(root, projection.anchor)
    try:
        live = capture_tree(root)
    except WorkspaceError as exc:
        raise WorkspaceChanged("Candidate changed before publication") from exc
    if live != projection.capture:
        raise WorkspaceChanged("Candidate changed before publication")

    staged = dict(projection.capture)
    for change in admitted:
        if change.action == "delete":
            staged.pop(change.path, None)
            continue
        content = change.content or b""
        if change.action == "update":
            prior = projection.capture.get(change.path)
            mode = prior.mode if prior is not None else PROJECTED_FILE_MODE
        else:
            mode = change.mode if change.mode is not None else PROJECTED_FILE_MODE
        staged[change.path] = FileState(mode, content, "file")
    _replace_tree(root, projection.anchor, staged)


def discard(projection: Projection) -> None:
    """Remove the private projection; the real Candidate is never touched.

    A frozen projection (`freeze_projection`) has directories at `0o500`, and
    an actor may lock or nest its projection however it likes, so the contents
    are removed by `_remove_contents` -- iterative, no-follow, restoring each
    directory to `0o700` before emptying it -- and only then does
    `TemporaryDirectory.cleanup()` remove the emptied private directory itself.
    Python 3.11's `os.walk` and `shutil.rmtree` recurse once per level, so a
    tree deeper than the recursion limit used to raise an unclassified
    `RecursionError` here (H11 review A1). `OSError` stays suppressed as
    before: an entry that cannot be removed is left, never followed. After a
    clean removal `cleanup()` only removes an empty directory; it meets a deep
    tree only when the removal already left a branch it could not enter (an
    unreadable directory where no-follow chmod is unsupported, or a level
    whose identity changed), and then 3.11's recursive
    `rmtree` would raise `RecursionError` instead of the `OSError` a shallow
    leftover gives, so that is suppressed with it: the entries stay, as for
    any `OSError`.
    """

    with contextlib.suppress(OSError):
        _remove_contents(Path(projection._tmp.name))
    with contextlib.suppress(OSError, RecursionError):
        projection._tmp.cleanup()


def discard_directory(directory: Path) -> None:
    """Discard a disposable operation tree with the projection's no-follow walk."""
    with contextlib.suppress(OSError):
        _remove_contents(directory)
    with contextlib.suppress(OSError):
        directory.rmdir()


#: The most directory descriptors `_remove_contents` holds at once, whatever the depth.
_REMOVAL_DESCRIPTORS = 64


@dataclass
class _RemovalFrame:
    """One directory level of `_remove_contents`: how to find it again and what is left."""

    name: str
    identity: tuple[int, int]
    descriptor: int | None
    pending: list[str]


def _remove_contents(directory: Path) -> None:
    """Remove everything below `directory`, without recursion and without following a link.

    An explicit stack of directory frames replaces the call stack. Each
    subdirectory is opened by name relative to its parent's descriptor with
    `O_NOFOLLOW | O_DIRECTORY`, so a symlink -- planted, or swapped in after
    the listing -- is never entered; it, a FIFO, a socket, a hard link and
    every other non-directory are unlinked by name.

    At most `_REMOVAL_DESCRIPTORS` descriptors below `directory` are held, so
    a tree deeper than the process's open-file limit still goes. The held
    levels are always the deepest ones, a contiguous run up to the top of the
    stack: past the bound the shallowest of them is closed, keeping its
    `(st_dev, st_ino)`. Climbing back to a closed level, the walk opens `..`
    from the level below it -- never a link -- before closing that one, and
    requires the identity it recorded, so it keeps removing the directory it
    listed, as a held descriptor would, even if that one was renamed. The
    walker restores its own level to `0o700` first, so a stripped search bit
    does not refuse `..` either. A `..` that still cannot be opened, or that
    is another directory (the level below was moved elsewhere), is refused;
    the level is then reopened by name from the root, each level no-follow
    and verified, and a level that cannot be reopened so ends its branch,
    which is left for the caller's `TemporaryDirectory.cleanup()`.

    Reopening by name costs the depth, and an actor still running beside the
    walk can force a refusal on every climb, so those reopens are budgeted:
    together they may open no more levels than the walk has opened going
    down. A reopen past the budget is not tried; its branch is left the same
    way. Going down opens each level once and climbing opens `..` at most
    once per level, so the whole removal stays linear in the number of
    directories, tampered with or not.

    Each directory is restored to `0o700` through its own descriptor before it
    is listed (as `TemporaryDirectory`'s cleanup does on a `PermissionError`).
    Only a directory without the read bit, which cannot be opened, is first
    chmodded by name with `follow_symlinks=False`; where that is unsupported
    (always for a link; for every entry on a C library without no-follow
    chmod, glibc before 2.32) CPython raises `NotImplementedError`, or
    `ValueError` with `dir_fd`, and the entry is left as it is. Every step
    carries its own `suppress`, so one entry that cannot be removed does not
    stop the rest, and every descriptor is closed whatever happens.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )

    def opened(name: str | Path, parent: int | None) -> int:
        try:
            return os.open(name, flags, dir_fd=parent)
        except PermissionError:
            with contextlib.suppress(OSError, NotImplementedError, ValueError):
                os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
            return os.open(name, flags, dir_fd=parent)

    def identity(descriptor: int) -> tuple[int, int]:
        details = os.fstat(descriptor)
        return (details.st_dev, details.st_ino)

    def verified(name: str, parent: int, expected: tuple[int, int]) -> int | None:
        """`name` under `parent`, opened no-follow, if it is still the directory recorded."""

        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except OSError:
            return None
        try:
            if identity(descriptor) == expected:
                return descriptor
        except OSError:
            pass
        os.close(descriptor)
        return None

    def listed(descriptor: int) -> list[str]:
        """Restore, list and unlink the non-directories; return the subdirectories."""

        with contextlib.suppress(OSError):
            os.fchmod(descriptor, 0o700)
        subdirectories: list[str] = []
        with contextlib.suppress(OSError), os.scandir(descriptor) as entries:
            for entry in entries:
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_directory = False
                if is_directory:
                    subdirectories.append(entry.name)
                    continue
                with contextlib.suppress(OSError):
                    os.unlink(entry.name, dir_fd=descriptor)
        return subdirectories

    def release(frame: _RemovalFrame) -> None:
        if frame.descriptor is not None:
            descriptor, frame.descriptor = frame.descriptor, None
            with contextlib.suppress(OSError):
                os.close(descriptor)

    #: `stack[first:]` are the held levels below the root; empty when `first == len(stack)`.
    first = 1

    #: Levels opened going down, and levels reopened by name: the second may
    #: not outgrow the first, so a racing actor cannot make the walk quadratic.
    descended = 0
    reopened = 0

    def drop(index: int) -> None:
        """Leave `stack[index:]` on disk for `cleanup()`; the level above stays the top."""

        nonlocal first
        for abandoned in stack[index:]:
            release(abandoned)
        del stack[index:]
        top = len(stack) - 1
        first = top if top >= 1 and stack[top].descriptor is not None else len(stack)

    def by_name(target: int) -> bool:
        """Reopen `stack[target]` from the root, level by level, within the budget.

        Only when no level between the root and `target` is held, which the
        contiguous run guarantees whenever the level above the run is needed.
        Each reopened level is held in its own frame until the next one is
        open, so the `finally` below closes it whatever happens. A level that
        cannot be reopened, or a reopen past the budget, drops its branch.
        """

        nonlocal first, reopened
        if reopened + target > descended:
            drop(target)
            return False
        for index in range(1, target + 1):
            parent = stack[index - 1].descriptor
            assert parent is not None
            reopened += 1
            descriptor = verified(stack[index].name, parent, stack[index].identity)
            if descriptor is None:
                drop(index)
                return False
            stack[index].descriptor = descriptor
            if index > 1:
                release(stack[index - 1])
        first = target
        return True

    root = opened(directory, None)
    #: The root is always held and never reopened, so it needs no identity.
    stack: list[_RemovalFrame] = [_RemovalFrame("", (0, 0), root, [])]
    try:
        stack[0].pending.extend(listed(root))
        while stack:
            top = len(stack) - 1
            if stack[top].descriptor is None and not by_name(top):
                continue
            frame = stack[top]
            descriptor = frame.descriptor
            assert descriptor is not None
            if frame.pending:
                name = frame.pending.pop()
                if len(stack) - first >= _REMOVAL_DESCRIPTORS and first < top:
                    # Make room first, so the bound holds even while opening.
                    release(stack[first])
                    first += 1
                try:
                    child = opened(name, descriptor)
                except OSError:
                    # Not a directory any more (a link swapped in): unlink it.
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=descriptor)
                    continue
                descended += 1
                stack.append(_RemovalFrame(name, (0, 0), child, []))
                stack[-1].identity = identity(child)
                stack[-1].pending.extend(listed(child))
                continue
            if top == 0:
                release(frame)
                stack.pop()
                continue
            above = stack[top - 1]
            if above.descriptor is None:
                # `..` of a directory is never a link; it must still be `above`.
                # The search bit on this level is what `..` needs.
                with contextlib.suppress(OSError):
                    os.fchmod(descriptor, 0o700)
                above.descriptor = verified("..", descriptor, above.identity)
                if above.descriptor is not None:
                    first = top - 1
            release(frame)
            stack.pop()
            first = min(first, len(stack))
            if above.descriptor is None and not by_name(top - 1):
                continue
            with contextlib.suppress(OSError):
                os.rmdir(frame.name, dir_fd=above.descriptor)
    finally:
        for frame in stack:
            release(frame)


def self_check(root: Path, changed: Sequence[str]) -> tuple[str, ...]:
    """Parse the changed YAML and compile the changed Python, as `"<path>: <error>"`."""

    issues: list[str] = []
    for relative in sorted(set(changed)):
        lowered = relative.casefold()
        if lowered.endswith((".yaml", ".yml")):
            try:
                yaml.safe_load((root / relative).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                issues.append(f"{relative}: {exc}")
        elif lowered.endswith(".py"):
            try:
                compile((root / relative).read_text(encoding="utf-8"), relative, "exec")
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
                issues.append(f"{relative}: {exc}")
    return tuple(issues)


@dataclass
class AttemptLog:
    """An owner-only `.jsonl` sink; every record passes through `safe_record`."""

    path: Path
    _handle: IO[bytes]
    _secrets: tuple[str, ...]

    def record(self, value: Mapping[str, Any]) -> None:
        """Append one redacted JSON line."""

        payload = safe_record(dict(value), self._secrets)
        self._handle.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> AttemptLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_attempt_log(
    log_root: Path,
    attempt_id: str,
    *,
    secrets: Sequence[str],
    admitted: Collection[str] = (),
) -> AttemptLog:
    """Open `<log_root>/<attempt_id>.jsonl` privately, re-admitting the root first.

    The attempt pathname is predictable and `run_dir_state` admits a run
    directory whose `editor-logs` already holds entries, so the descriptor is
    *proved* by `open_private_file` before anything is written through it, the
    way C1's journal proves its own private files.

    `admitted` is the in-process role's own `admitted_hygiene`: the instance
    digests of the hygiene findings the operator accepted (PDR-0010). An
    accepted finding is admitted here rather than refused; everything else
    refuses as before, and the empty default keeps a caller without a decision
    on exactly the old behaviour.
    """

    from evolution.tree.execution.output_admission import (
        admit_output_root,  # circular at module scope
    )

    if not attempt_id or "/" in attempt_id or attempt_id in {os.curdir, os.pardir}:
        raise ValueError("attempt_id must be one path segment")
    admit_output_root(log_root, admitted=admitted)
    root = ensure_private_directory(log_root)
    name = f"{attempt_id}.jsonl"
    path = root / name
    descriptor = open_private_file(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        refusal=f"attempt log is not a private regular file: {name}",
    )
    return AttemptLog(path, os.fdopen(descriptor, "wb", buffering=0), tuple(secrets))


def bounded_note(text: str) -> str:
    """Cut a note to `MAX_NOTE_BYTES` on a character boundary."""

    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_NOTE_BYTES:
        return text
    return encoded[:MAX_NOTE_BYTES].decode("utf-8", "ignore")


def _directory_identity(path: Path) -> tuple[int, int]:
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise WorkspaceError(f"workspace path is not a directory: {path}")
    return details.st_dev, details.st_ino


def _has_identity(path: Path, expected: tuple[int, int]) -> bool:
    try:
        return _directory_identity(path) == expected
    except (OSError, WorkspaceError):
        return False


def _file_identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _writable(relative: str, rules: Sequence[str]) -> bool:
    """Match one relative path against the operator's writable set (policy grammar)."""

    path = PurePosixPath(relative)
    return any(policy_matches(rule, path, False) for rule in rules)


def _is_control(relative: str, prefix: str | None) -> bool:
    return prefix is not None and (relative == prefix or relative.startswith(f"{prefix}/"))


def _without_control(capture: Capture, prefix: str | None) -> dict[str, FileState]:
    return {
        relative: entry for relative, entry in capture.items() if not _is_control(relative, prefix)
    }


def _control_subtree(capture: Capture, prefix: str | None) -> dict[str, FileState]:
    return {relative: entry for relative, entry in capture.items() if _is_control(relative, prefix)}


def _normalized(capture: Capture) -> dict[str, FileState]:
    """The capture as the projection is written: same bytes, every file at `0600`."""

    return {
        relative: entry
        if entry.kind == "dir"
        else FileState(PROJECTED_FILE_MODE, entry.content, "file")
        for relative, entry in capture.items()
    }


def _file_changes(baseline: Capture, current: Capture) -> tuple[Change, ...]:
    """The file-level diff of two captures; a kind flip reads as a deletion."""

    changes: list[Change] = []
    for relative in sorted(set(baseline) | set(current)):
        if relative == _ROOT:
            continue
        prior, now = baseline.get(relative), current.get(relative)
        if prior == now:
            continue
        prior_file = prior if prior is not None and prior.kind == "file" else None
        now_file = now if now is not None and now.kind == "file" else None
        if prior_file is None and now_file is None:
            continue
        if prior_file is None:
            assert now_file is not None
            changes.append(Change(relative, "create", now_file.content, now_file.mode))
        elif now_file is None:
            changes.append(Change(relative, "delete", None, None))
        else:
            changes.append(Change(relative, "update", now_file.content, None))
    return tuple(changes)


def _materialize(state: Capture, destination: Path) -> None:
    """Write one capture into an existing empty directory, modes last.

    Directories are made at `0700` so the files can be written into them, then
    chmod'ed deepest-first to the mode the capture carries. A directory the
    state needs but does not name -- the parent of a file the actor created --
    takes the root's mode.
    """

    root_mode = state[_ROOT].mode if _ROOT in state else 0o700
    known = {
        relative: entry.mode
        for relative, entry in state.items()
        if relative != _ROOT and entry.kind == "dir"
    }
    made: list[str] = []

    def ensure(relative: str) -> None:
        if relative in {"", _ROOT} or relative in made:
            return
        ensure(str(PurePosixPath(relative).parent))
        (destination / relative).mkdir(mode=0o700, exist_ok=True)
        made.append(relative)

    for relative in sorted(known):
        ensure(relative)
    for relative in sorted(state):
        entry = state[relative]
        if relative == _ROOT or entry.kind != "file":
            continue
        ensure(str(PurePosixPath(relative).parent))
        path = destination / relative
        path.write_bytes(entry.content)
        os.chmod(path, entry.mode)
    for relative in sorted(made, key=lambda value: value.count("/"), reverse=True):
        os.chmod(destination / relative, known.get(relative, root_mode))
    os.chmod(destination, root_mode)


def _replace_tree(root: Path, anchor: WorkspaceAnchor, state: Capture) -> None:
    """Swap a freshly staged tree in for the Candidate root, restoring it on failure."""

    assert_anchored(root, anchor)
    parent = root.parent
    stage = Path(tempfile.mkdtemp(prefix=".evo-editor-publish-", dir=parent))
    backup = Path(tempfile.mkdtemp(prefix=".evo-editor-backup-", dir=parent))
    backup.rmdir()
    try:
        _materialize(state, stage)
        assert_anchored(root, anchor)
        os.rename(root, backup)
        if not _has_identity(backup, anchor.root):
            if not os.path.lexists(root):
                os.rename(backup, root)
            raise WorkspaceChanged("Candidate workspace root changed during publication")
        try:
            root.mkdir(mode=0o700)
        except OSError as exc:
            if not os.path.lexists(root) and _has_identity(backup, anchor.root):
                with contextlib.suppress(OSError):
                    os.rename(backup, root)
            raise WorkspaceChanged(
                "Candidate workspace root reappeared during publication"
            ) from exc
        reservation = _directory_identity(root)
        try:
            os.rename(stage, root)
        except BaseException:
            if _has_identity(root, reservation):
                with contextlib.suppress(OSError):
                    os.rename(backup, root)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
