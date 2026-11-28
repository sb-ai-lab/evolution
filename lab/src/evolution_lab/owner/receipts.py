"""Bounded private JSON and immutable scope evidence; no database authority."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from evolution_lab.durability import sync_file


MAX_RECEIPT_BYTES = 16384
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ReceiptError(ValueError):
    """Evidence is malformed, oversized, or bound to a different scope."""


def validate_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ReceiptError('invalid scope identity')
    return value


def open_directory_nofollow(path):
    """Open an existing directory without following any path component."""
    path = Path(path)
    if '..' in path.parts:
        raise ReceiptError('parent traversal is not a control path')
    path = path.absolute()
    descriptor = os.open('/', _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError('duplicate JSON key')
        result[key] = value
    return result


def _constant(_):
    raise ReceiptError('non-finite JSON number')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ReceiptError('non-finite JSON number')
    return number


def decode_json(raw, *, max_bytes=MAX_RECEIPT_BYTES):
    if not raw or len(raw) > max_bytes:
        raise ReceiptError('invalid JSON size')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReceiptError('invalid JSON') from exc
    if not isinstance(value, dict):
        raise ReceiptError('JSON must be an object')
    return value


def encode_json(payload, *, max_bytes=MAX_RECEIPT_BYTES):
    if not isinstance(payload, dict):
        raise ReceiptError('JSON must be an object')
    try:
        raw = json.dumps(payload, allow_nan=False, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ReceiptError('invalid JSON') from exc
    if not raw or len(raw) > max_bytes:
        raise ReceiptError('invalid JSON size')
    return raw


def read_json_at(directory_fd, name, *, max_bytes=MAX_RECEIPT_BYTES):
    if not name or '/' in name or name in {'.', '..'}:
        raise ReceiptError('invalid evidence filename')
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ReceiptError('invalid evidence file')
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(descriptor, max_bytes + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        return decode_json(raw, max_bytes=max_bytes)
    finally:
        os.close(descriptor)


def read_json(path, *, max_bytes=MAX_RECEIPT_BYTES):
    path = Path(path)
    directory_fd = open_directory_nofollow(path.parent)
    try:
        return read_json_at(directory_fd, path.name, max_bytes=max_bytes)
    finally:
        os.close(directory_fd)


def publish_json_at(directory_fd, name, payload, *, max_bytes=MAX_RECEIPT_BYTES):
    """fsync a fresh 0600 file, link without replacing, then fsync directory."""
    if not name or '/' in name or name in {'.', '..'}:
        raise ReceiptError('invalid evidence filename')
    raw = encode_json(payload, max_bytes=max_bytes)
    temporary = '.receipt-' + uuid.uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(raw)
        while remaining:
            count = os.write(descriptor, remaining)
            remaining = remaining[count:]
        sync_file(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=directory_fd)
    sync_file(directory_fd)


def publish_json(path, payload, *, max_bytes=MAX_RECEIPT_BYTES):
    path = Path(path)
    directory_fd = open_directory_nofollow(path.parent)
    try:
        publish_json_at(directory_fd, path.name, payload, max_bytes=max_bytes)
    finally:
        os.close(directory_fd)


def validate_cleanup_receipt(value, scope_id, owner_generation):
    validate_id(scope_id)
    validate_id(owner_generation)
    keys = {'schema', 'scope_id', 'owner_generation', 'child_launched', 'child_exit_code',
        'process_tree_empty', 'forced', 'spawn_closed'}
    if not isinstance(value, dict):
        raise ReceiptError('invalid cleanup receipt shape')
    schema = value.get('schema')
    if schema == 'lab-scope-cleanup/2':
        keys = keys | {'ownership'}
    if set(value) != keys:
        raise ReceiptError('invalid cleanup receipt shape')
    if (schema not in ('lab-scope-cleanup/1', 'lab-scope-cleanup/2') or value['scope_id'] != scope_id
            or value['owner_generation'] != owner_generation):
        raise ReceiptError('cleanup receipt identity mismatch')
    if schema == 'lab-scope-cleanup/2' and value['ownership'] not in ('subreaper', 'group_tree_marker'):
        raise ReceiptError('invalid cleanup ownership')
    for field in ('child_launched', 'process_tree_empty', 'forced', 'spawn_closed'):
        if type(value[field]) is not bool:
            raise ReceiptError('invalid cleanup boolean')
    code = value['child_exit_code']
    if code is not None and type(code) is not int:
        raise ReceiptError('invalid exit code')
    if not value['process_tree_empty'] or not value['spawn_closed']:
        raise ReceiptError('cleanup is not terminal')
    if not value['child_launched'] and code is not None:
        raise ReceiptError('unlaunched child has an exit code')
    return dict(value, ownership=value.get('ownership', 'subreaper'))


def read_cleanup_receipt(scope_directory, scope_id, owner_generation):
    return validate_cleanup_receipt(read_json(Path(scope_directory) / 'cleanup.receipt'),
        scope_id, owner_generation)


def validate_cleanup_request(value, scope_id, owner_generation):
    if not isinstance(value, dict) or set(value) != {'schema', 'scope_id', 'owner_generation', 'request_id'}:
        raise ReceiptError('invalid cleanup request shape')
    if (value['schema'] != 'lab-scope-stop/1' or value['scope_id'] != validate_id(scope_id)
            or value['owner_generation'] != validate_id(owner_generation)):
        raise ReceiptError('cleanup request identity mismatch')
    validate_id(value['request_id'])
    return dict(value)


def publish_cleanup_request(scope_directory, scope_id, owner_generation):
    payload = dict(schema='lab-scope-stop/1', scope_id=validate_id(scope_id),
        owner_generation=validate_id(owner_generation), request_id=uuid.uuid4().hex)
    directory_fd = open_directory_nofollow(scope_directory)
    try:
        try:
            publish_json_at(directory_fd, 'cleanup.request', payload)
        except FileExistsError:
            return validate_cleanup_request(read_json_at(directory_fd, 'cleanup.request'), scope_id, owner_generation)
        return payload
    finally:
        os.close(directory_fd)
