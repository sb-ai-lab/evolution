"""Read-only selected-artifact resolution and per-call verified snapshots.

Call ``snapshot_changes`` inside Owner-held ``_result_thread`` work, and build the
entire diff/archive inside its context. The caller owns admission and the read
lease. This module never starts/resumes a campaign or mutates a core journal.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from types import MappingProxyType
from typing import Any, Iterator, Mapping
from uuid import uuid4

from evolution_lab._paths import canonical_root

from evolution.tree import (
    ArtifactError, ArtifactStore, JournalBoundaryError,
)
from evolution.tree.artifacts.artifact_policy import TreeScan, entry_set, scan_tree
from .core_raw import FAMILIES, campaign_read, CoreRawUnavailable
from evolution.tree.evolve import derive_run_id

from .binding import open_prepared
from .models import TREE_REF, OwnerError


class _Unavailable(ValueError):
    pass


class _ProjectionError(Exception):
    """Carry unexpected reader errors through local artifact guard mapping."""

    def __init__(self, error):
        self.error = error


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    data: bytes
    executable: int


@dataclass(frozen=True, slots=True)
class ChangeSnapshot:
    """Verified immutable bytes and public identities for one diff/export call.

    File maps contain artifact files only. Dependencies are copied and verified
    but never exposed as payloads. Directory entries remain in the public scans.
    ``changed_paths`` is sorted and policy-checked; Task3 owns change encoding.
    """

    operation_id: str
    prepared_digest: str
    bundle_digest: str
    distributions: tuple[Mapping[str, str], ...]
    source_digest: str
    selected_digest: str
    selected_version: str | None
    campaign_outcome: Mapping[str, Any] | None
    campaign_complete: bool
    measurement_complete: bool
    source_scan: TreeScan
    selected_scan: TreeScan
    source_files: Mapping[str, SnapshotFile]
    selected_files: Mapping[str, SnapshotFile]
    changed_paths: tuple[str, ...]
    call_directory: Path
    source_ref: str
    selected_ref: str

    @property
    def no_change(self) -> bool:
        return (self.source_digest == self.selected_digest
            and self.source_scan.dependency_digest == self.selected_scan.dependency_digest)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _relative(value: str) -> tuple[str, ...]:
    if (not isinstance(value, str) or not value or '\\' in value or '\0' in value
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise _Unavailable()
    return tuple(value.split('/'))


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Open every absolute-path component with O_NOFOLLOW, including the leaf."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise _Unavailable()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _relative_directory(parent: int, path: str) -> Iterator[int]:
    fd = os.dup(parent)
    try:
        for part in _relative(path):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _regular_bytes(parent: int, name: str) -> tuple[bytes, int]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _Unavailable()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as stream:
        actual = os.fstat(stream.fileno())
        if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1
                or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)):
            raise _Unavailable()
        data = stream.read()
        after = os.fstat(stream.fileno())
        if (after.st_nlink != 1 or actual.st_size != len(data)
                or (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise _Unavailable()
        return data, int(bool(actual.st_mode & 0o111))


def _copy_tree(source_fd: int, destination_fd: int, policy, *, published: bool, prefix=''):
    """Copy only included entries through directory descriptors, without links."""
    for name in sorted(os.listdir(source_fd)):
        relative = f'{prefix}/{name}' if prefix else name
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        is_directory = stat.S_ISDIR(info.st_mode)
        if policy.is_excluded(PurePosixPath(relative), is_directory):
            if published:
                raise _Unavailable()
            continue
        _relative(relative)
        if is_directory:
            os.mkdir(name, 0o700, dir_fd=destination_fd)
            with _relative_directory(source_fd, name) as source_child:
                with _relative_directory(destination_fd, name) as destination_child:
                    _copy_tree(source_child, destination_child, policy,
                        published=published, prefix=relative)
        elif stat.S_ISREG(info.st_mode):
            data, executable = _regular_bytes(source_fd, name)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o700 if executable else 0o600, dir_fd=destination_fd)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), 0o700 if executable else 0o600)
        else:
            raise _Unavailable()


def _file_map(root_fd: int, scan: TreeScan) -> Mapping[str, SnapshotFile]:
    result = {}
    for entry in scan.artifact:
        parts = _relative(entry.path)
        if entry.kind != 'file':
            continue
        with ExitStack() as stack:
            parent = (stack.enter_context(_relative_directory(root_fd, '/'.join(parts[:-1])))
                if len(parts) > 1 else root_fd)
            data, executable = _regular_bytes(parent, parts[-1])
            if executable != int(entry.executable) or len(data) != entry.size:
                raise _Unavailable()
            result[entry.path] = SnapshotFile(data, executable)
    return MappingProxyType(result)


def parse_tree_ref(value):
    """``initial`` | ``champion:vN`` | ``attempt:<attempt_id>`` as ``(kind, name)``."""
    if not isinstance(value, str) or TREE_REF.fullmatch(value) is None:
        raise OwnerError('invalid_argument', 'diff')
    if value == 'initial':
        return 'initial', None
    kind, _, name = value.partition(':')
    return kind, name


def _published(entry, lineage_run_id, locator):
    """The entry's public reference must name exactly its own published copy."""
    reference = entry.reference
    _relative(reference.locator)
    if (reference.run_id != lineage_run_id
            or reference.locator != locator
            or reference.content_digest != entry.candidate_digest
            or reference.dependency_digest != entry.dependency_digest):
        raise _Unavailable()
    return entry


def _champion(projection, champions, lineage_run_id):
    if not isinstance(projection, dict):
        raise _Unavailable()
    matches = [entry for entry in champions
        if (entry.version_id, entry.candidate_digest)
        == (projection.get('version'), projection.get('digest'))]
    if len(matches) != 1:
        raise _Unavailable()
    entry = matches[0]
    return _published(entry, lineage_run_id, f'artifacts/champions/{entry.version_id}')


def _selected_tree(kind, name, result, lineage):
    """The public lineage entry of one parsed tree ref; its ``reference.locator`` is the
    only source of the tree's run-relative path (``initial`` reads the Bundle seed, whose
    identity is the initial champion's). ``champion`` without a name is the final champion."""
    if kind == 'initial':
        return _champion(result['initial_champion'], lineage.champions, lineage.run_id)
    if kind == 'champion':
        if name is None:
            return _champion(result['final_champion'], lineage.champions, lineage.run_id)
        matches = [entry for entry in lineage.champions if entry.version_id == name]
        if not matches:
            raise OwnerError('not_found', 'diff')
        if len(matches) != 1:
            raise _Unavailable()
        return _published(matches[0], lineage.run_id, f'artifacts/champions/{name}')
    if kind != 'attempt':
        raise OwnerError('invalid_argument', 'diff')
    matches = [entry for entry in lineage.candidates if entry.attempt_id == name]
    if not matches:
        raise OwnerError('not_found', 'diff')
    if len(matches) != 1:
        raise _Unavailable()
    entry = matches[0]
    if entry.reference is None or entry.corruption is not None:
        raise OwnerError('tree_unavailable', 'diff')
    return _published(entry, lineage.run_id, f'artifacts/candidates/{name}')


def _changed_files(source_scan, selected_scan, source_files, selected_files, policy):
    before = {entry.path: entry for entry in source_scan.artifact}
    after = {entry.path: entry for entry in selected_scan.artifact}
    for path in before.keys() & after.keys():
        if before[path].kind != after[path].kind:
            raise _Unavailable()  # file/directory replacement is outside file-only /1
    for path in before.keys() ^ after.keys():
        entry = before.get(path) or after[path]
        files = source_files if path in before else selected_files
        if entry.kind == 'directory' and not any(name.startswith(path + '/') for name in files):
            raise _Unavailable()  # an added/removed empty directory has no file representation
    changed = tuple(sorted(path for path in source_files.keys() | selected_files.keys()
        if source_files.get(path) != selected_files.get(path)))
    if any(not policy.is_writable(PurePosixPath(path)) for path in changed):
        raise _Unavailable()
    if not changed and source_scan.candidate_digest != selected_scan.candidate_digest:
        raise _Unavailable()
    return changed


@contextmanager
def snapshot_changes(state_directory, preparation_storage, status, inventory, *, stage='diff',
                     source='initial', selected=None, scratch_directory: Path | None = None):
    """Resolve a saved campaign and yield one fresh identity-checked snapshot.

    ``inventory`` must be the operation's durable ``owned_inventory`` result;
    operation/root topology is checked by name, never inferred by tuple position.
    Caller must hold its Task1 shared read/export lease through this whole context.
    Cleanup runs after the caller's serialization, including exceptional exits.
    ``source``/``selected`` are tree refs (``parse_tree_ref``); ``selected=None`` is
    the final champion, the only pair an export (``lab-change-set/1``) ever reads.
    """
    if stage not in ('diff', 'export'):
        raise OwnerError('invalid_argument', 'read')
    refs = (parse_tree_ref(source), None if selected is None else parse_tree_ref(selected))
    try:
        with _snapshot_changes(state_directory, preparation_storage, status, inventory,
                               source, selected, refs, scratch_directory) as value:
            yield value
    except _ProjectionError as error:
        raise error.error from None
    except OwnerError as error:
        if error.stage == stage:
            raise
        raise OwnerError('export_unavailable', stage) from None
    except OSError as error:
        code = ('export_unavailable' if error.errno in
            (errno.ENOENT, errno.ELOOP, errno.ENOTDIR) else 'io_error')
        raise OwnerError(code, stage) from None
    except (ArtifactError, JournalBoundaryError, CoreRawUnavailable, ValueError, TypeError, KeyError, AttributeError):
        raise OwnerError('export_unavailable', stage) from None


def _open_tree(stack, run_fd, kind, entry):
    """Open a published tree through its lineage locator; an attempt's absent copy is
    ``tree_unavailable``, never a path Lab assembles itself."""
    try:
        return stack.enter_context(_relative_directory(run_fd, entry.reference.locator))
    except (FileNotFoundError, NotADirectoryError):
        if kind == 'attempt':
            raise OwnerError('tree_unavailable', 'diff') from None
        raise


@contextmanager
def _snapshot_changes(state_directory, preparation_storage, status, inventory,
                      source_ref, selected_ref, refs, scratch_directory):
    (source_kind, source_name), selected_parsed = refs
    selected_kind, selected_name = selected_parsed or ('champion', None)
    if status.kind != 'campaign':
        raise _Unavailable()
    operation_parts = _relative(status.id)
    if len(operation_parts) != 1:
        raise _Unavailable()
    expected_root = f'artifacts/{status.id}'
    trees = [item['path'] for item in inventory if item['kind'] == 'tree']
    if trees != [expected_root]:
        raise _Unavailable()
    _relative(expected_root)
    root = Path(state_directory) / expected_root
    run = root / status.id
    binding = open_prepared(preparation_storage, status.prepared_digest, stage='read')
    if binding.kind != 'campaign' or binding.prepared_digest != status.prepared_digest:
        raise _Unavailable()
    policy = binding.bundle.manifest.policy
    seed = Path(binding.bundle.manifest.seed_root)
    # Resolve only the Bundle's external prefix. Seed components below that
    # admitted root still pass through the unchanged nofollow walker.
    seed_relative = seed.relative_to(binding.bundle.bundle_path)
    seed = canonical_root(binding.bundle.bundle_path) / seed_relative
    if (root / 'linked.json').is_file() and (root / 'linked-seed').exists():
        # The public store verification below independently binds this copied
        # seed to the run's initial Champion, including dependency bytes.
        seed = root / 'linked-seed'
    with ExitStack() as stack:
        root_fd = stack.enter_context(_directory(root))
        run_fd = stack.enter_context(_relative_directory(root_fd, status.id))
        try:
            read = campaign_read(run, families=FAMILIES, with_result=True)
            campaign = read.status.to_payload()
            result = read.result.to_payload()
        except (TypeError, KeyError, AttributeError) as error:
            # These are implementation failures, not malformed artifact guards.
            raise _ProjectionError(error) from error
        if (campaign['run_id'] != derive_run_id(run)
                or any(campaign[key] != result[key] for key in
                    ('run_id', 'final_champion', 'complete', 'campaign_outcome'))):
            raise _Unavailable()
        # Artifact ledger IDs are a separate namespace from campaign run IDs.
        # A published side supplies bytes from its lineage locator; ``initial``
        # identity comes from the lineage metadata and the independently verified
        # saved Bundle seed. Open published reference ancestry before the
        # verifying public read.
        sides = ((source_kind, source_name), (selected_kind, selected_name))
        observed = ArtifactStore.inspect(run, verify=False)
        observed_entries = [_selected_tree(kind, name, result, observed) for kind, name in sides]
        fds = [stack.enter_context(_directory(seed)) if kind == 'initial'
               else _open_tree(stack, run_fd, kind, entry)
               for (kind, _), entry in zip(sides, observed_entries)]
        lineage = ArtifactStore.inspect(run, verify=True)
        if lineage.run_id != observed.run_id:
            raise _Unavailable()
        entries = [_selected_tree(kind, name, result, lineage) for kind, name in sides]
        for (kind, _), entry, seen in zip(sides, entries, observed_entries):
            if kind != 'initial' and (entry.corruption is not None or entry.reference != seen.reference):
                raise _Unavailable()
        (source_entry, selected_entry), (source_fd, selected_fd) = entries, fds
        if source_entry.dependency_digest != selected_entry.dependency_digest:
            raise _Unavailable()
        published = [None if kind == 'initial' else entry_set(run / entry.reference.locator)
                     for (kind, _), entry in zip(sides, entries)]
        staging_root = root if scratch_directory is None else Path(scratch_directory)
        staging_root_fd = root_fd if scratch_directory is None else stack.enter_context(_directory(staging_root))
        try:
            os.mkdir('staging', 0o700, dir_fd=staging_root_fd)
        except FileExistsError:
            pass
        staging_fd = stack.enter_context(_relative_directory(staging_root_fd, 'staging'))
        while True:
            call_id = uuid4().hex
            try:
                os.mkdir(call_id, 0o700, dir_fd=staging_fd)
                break
            except FileExistsError:
                continue
        call_directory = staging_root / 'staging' / call_id
        try:
            with _relative_directory(staging_fd, call_id) as call_fd:
                os.mkdir('source', 0o700, dir_fd=call_fd)
                os.mkdir('selected', 0o700, dir_fd=call_fd)
                with _relative_directory(call_fd, 'source') as staged_source_fd:
                    with _relative_directory(call_fd, 'selected') as staged_selected_fd:
                        _copy_tree(source_fd, staged_source_fd, policy,
                            published=published[0] is not None)
                        _copy_tree(selected_fd, staged_selected_fd, policy,
                            published=published[1] is not None)
                        source_scan = scan_tree(call_directory / 'source', policy)
                        selected_scan = scan_tree(call_directory / 'selected', policy)
                        for scan, entry, entries_on_disk in zip((source_scan, selected_scan),
                                (source_entry, selected_entry), published):
                            if (scan.candidate_digest != entry.candidate_digest
                                    or scan.dependency_digest != entry.dependency_digest
                                    or (entries_on_disk is not None and entries_on_disk
                                        != {item.path for item in scan.artifact + scan.dependency})):
                                raise _Unavailable()
                        source_files = _file_map(staged_source_fd, source_scan)
                        selected_files = _file_map(staged_selected_fd, selected_scan)
                        changed = _changed_files(source_scan, selected_scan, source_files, selected_files, policy)
                        yield ChangeSnapshot(status.id, binding.prepared_digest,
                            binding.bundle.bundle_digest,
                            tuple(_freeze(pin.to_payload()) for pin in binding.bundle.execution_environment.distributions),
                            source_scan.candidate_digest, selected_scan.candidate_digest,
                            selected_entry.version_id if selected_kind == 'champion' else None,
                            _freeze(campaign['campaign_outcome']),
                            campaign['complete'], campaign['final_champion']['measurement_complete'],
                            source_scan, selected_scan, source_files, selected_files, changed, call_directory,
                            source_ref, selected_ref or f'champion:{selected_entry.version_id}')
        finally:
            # fd-relative rmtree refuses substituted symlink roots and never sweeps
            # neighbouring calls or crash leftovers. All caller IO has now settled.
            shutil.rmtree(call_id, dir_fd=staging_fd)
