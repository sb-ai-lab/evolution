"""Metadata only: nofollow inputs of the operation projection, never Core bytes."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .receipts import open_directory_nofollow

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _metadata(value):
    return (stat.S_IFMT(value.st_mode), value.st_size, value.st_mtime_ns,
            value.st_ino, value.st_ctime_ns)


def _path_signature(path, *, tree, ancestry=False):
    """Locate an optional root, then treat every failure within it as incomplete.

    Probe each component before opening it: absent roots are ordinary inputs,
    but disappearance between a successful stat and open is a failed signature.
    Directory descriptors prevent traversal through replaced symlink ancestors.
    """
    path = Path(path).absolute()
    if '..' in path.parts:
        raise OSError('parent traversal is not a projection path')
    directory = open_directory_nofollow(path.anchor)
    ancestors = []
    try:
        for part in path.parts[1:-1]:
            try:
                observed = os.stat(part, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return tuple(ancestors) + (('', 'absent'),)
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            try:
                if _metadata(os.fstat(child)) != _metadata(observed):
                    raise OSError('projection path changed while opening')
            except BaseException:
                os.close(child)
                raise
            os.close(directory)
            directory = child
            if ancestry:
                info = os.fstat(directory)
                # Identity/access changes matter; unrelated directory writes do
                # not change the bytes or admission of a named input file.
                ancestors.append((part, info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid))
        try:
            observed = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return tuple(ancestors) + (('', 'absent'),)
        rows = ancestors + [('', *_metadata(observed))]
        if tree:
            _walk(directory, path.name, observed, '', rows)
        return tuple(rows)
    finally:
        os.close(directory)


def input_signature(path):
    """A named evidence input and nofollow ancestry, or None on uncertain IO."""
    try:
        return _path_signature(path, tree=False, ancestry=True)
    except OSError:
        return None


def _walk(parent, name, observed, relative, rows):
    directory = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if _metadata(os.fstat(directory)) != _metadata(observed):
            raise OSError('projection directory changed while opening')
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            value = os.stat(name, dir_fd=directory, follow_symlinks=False)
            child = f'{relative}/{name}' if relative else name
            rows.append((child, *_metadata(value)))
            if stat.S_ISDIR(value.st_mode):
                _walk(directory, name, value, child, rows)
    finally:
        os.close(directory)


def run_signature(state_directory, operation_id, scope_directory):
    """Recursive run metadata plus the scope outcome, or None on uncertain IO.

    Symlink leaves are recorded, never followed; run and scope directory
    boundaries must themselves be nofollow directories. Missing roots/files
    have explicit markers. No content, clock, persistent state or size limit.
    """
    try:
        run = Path(state_directory) / 'artifacts' / operation_id / operation_id
        run_value = _path_signature(run, tree=True)
        scope_value = None if scope_directory is None else _path_signature(
            Path(scope_directory) / 'evaluation-outcome.json', tree=False)
        return run_value, scope_value
    except OSError:
        return None
