"""Private scratch pair paths and their durable binding, under the Owner flock."""
from __future__ import annotations

from hashlib import sha256
import logging
import os
from pathlib import Path
import stat
from uuid import uuid4

from evolution_lab._paths import canonical_root

from .models import OwnerError, ResourceRef
from .receipts import encode_json, open_directory_nofollow, read_json_at

from evolution_lab.durability import sync_file

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_log = logging.getLogger(__name__)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove a directory through nofollow descriptors, refusing a linked root."""
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        actual = os.fstat(descriptor)
        for child in os.listdir(descriptor):
            info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_tree_at(descriptor, child)
            else:
                os.unlink(child, dir_fd=descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino):
            raise OSError('scratch directory changed')
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(descriptor)


def remove_target_scratch(pair_fd: int, target_id: str) -> None:
    """Best effort after a durable stopped/confirmed/True write, never before it."""
    target_id = ResourceRef('target', target_id).id
    for container in ('targets', 'snapshots'):
        try:
            try:
                descriptor = os.open(container, _DIRECTORY_FLAGS, dir_fd=pair_fd)
            except FileNotFoundError:
                continue
            try:
                _remove_tree_at(descriptor, target_id)
            finally:
                os.close(descriptor)
        except OSError as error:
            _log.warning('Scratch %s/%s was not removed: %s', container, target_id, error)


def clean_scratch_directory(scratch: Path | None, target_ids, *, staging=False) -> None:
    """Retry confirmed stops and, under a newly acquired Owner flock, old calls."""
    if scratch is None:
        return
    try:
        pair_fd = open_directory_nofollow(scratch)
        try:
            for target_id in target_ids:
                remove_target_scratch(pair_fd, target_id)
            if staging:
                try:
                    staging_fd = os.open('staging', _DIRECTORY_FLAGS, dir_fd=pair_fd)
                except FileNotFoundError:
                    return
                try:
                    for call_id in os.listdir(staging_fd):
                        try:
                            _remove_tree_at(staging_fd, call_id)
                        except OSError as error:
                            _log.warning('Scratch staging/%s was not removed: %s', call_id, error)
                finally:
                    os.close(staging_fd)
        finally:
            os.close(pair_fd)
    except OSError as error:
        _log.warning('Scratch cleanup was incomplete: %s', error)


def scratch_pair_directory(scratch: Path, state: Path) -> Path:
    """Hash the absolute state path once, at the service-to-Owner boundary."""
    return Path(scratch) / sha256(str(Path(state).absolute()).encode()).hexdigest()[:16]


def _absolute(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or '\x00' in str(path)
            or str(path) != str(value)):
        raise ValueError('invalid scratch path')
    return path


def prepare_scratch_directory(path: Path) -> Path:
    """Canonicalize ancestors, then create a root through nofollow descriptors.

    Existing leaves must already belong to this user and have exactly mode 0700;
    never repair permissions on an operator-selected existing directory.
    """
    try:
        path = canonical_root(_absolute(path))
        descriptor = os.open('/', _DIRECTORY_FLAGS)
        try:
            for part in path.parts[1:]:
                try:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('scratch directory is not private')
        finally:
            os.close(descriptor)
        return path
    except (OSError, TypeError, ValueError):
        raise OwnerError('invalid_argument', 'connect') from None


class ScratchBindingError(OwnerError):
    """Startup diagnostic; the durable Owner failure keeps its fixed message."""

    def __init__(self, recorded):
        super().__init__('invalid_argument', 'connect')
        RuntimeError.__init__(self,
            f'Scratch directory still has target data or cannot be inspected: {recorded}')


def _refuse(recorded=None):
    return (OwnerError('invalid_argument', 'connect') if recorded is None
        else ScratchBindingError(recorded))


def _has_target_data(path):
    try:
        descriptor = open_directory_nofollow(path)
    except FileNotFoundError:
        return False  # tmpfs may have vanished since the preceding service run
    try:
        for name in ('targets', 'snapshots'):
            try:
                container = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                continue
            try:
                if os.listdir(container):
                    return True
            finally:
                os.close(container)
        return False
    finally:
        os.close(descriptor)


def bind_scratch(root_fd, state: Path, scratch: Path | None) -> Path | None:
    """Validate/rebind scratch.json only while the state root's flock is held.

    ``scratch`` is already the pair directory, never the operator root.
    """
    recorded = None
    try:
        if scratch is not None:
            scratch = canonical_root(_absolute(scratch))
            if scratch.is_relative_to(state) or state.is_relative_to(scratch):
                raise ValueError('overlapping state and scratch')
        try:
            marker = read_json_at(root_fd, 'scratch.json')
        except FileNotFoundError:
            marker = None
        if marker is not None:
            if (not isinstance(marker, dict) or set(marker) != {'schema', 'path'}
                    or marker['schema'] != 'lab-owner-scratch/1'
                    or not isinstance(marker['path'], str)):
                raise ValueError('invalid scratch marker')
            recorded = _absolute(marker['path'])
            if recorded.is_relative_to(state) or state.is_relative_to(recorded):
                raise ValueError('invalid recorded scratch path')
            if recorded != scratch and _has_target_data(recorded):
                raise _refuse(recorded)
        if scratch is not None:
            prepare_scratch_directory(scratch)
        if recorded == scratch:
            return scratch
        if scratch is None:
            os.unlink('scratch.json', dir_fd=root_fd)
            sync_file(root_fd)
        else:
            temporary = '.scratch-' + uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root_fd)
            try:
                raw = memoryview(encode_json({'schema': 'lab-owner-scratch/1', 'path': str(scratch)}))
                while raw:
                    raw = raw[os.write(descriptor, raw):]
                sync_file(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.replace(temporary, 'scratch.json', src_dir_fd=root_fd, dst_dir_fd=root_fd)
                sync_file(root_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
        return scratch
    except (OSError, TypeError, ValueError):
        raise _refuse(recorded) from None
