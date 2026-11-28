"""Write new private artifacts; publish manifest only after all rows exist."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import ObservationError

from evolution_lab.durability import sync_file

FILES = {'lab-observations/1': ('spans.jsonl', 'executions.jsonl'),
         'lab-basket/1': ('cases.jsonl',)}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def clean_json(value):
    return json.loads(json_bytes(value))


def revision(): return uuid.uuid4().hex


def write_bytes(path: Path, data: bytes):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        sync_file(stream.fileno())


def write_artifact(path, manifest, records):
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
        files = {}
        for name, rows in records.items():
            if name not in FILES[manifest['schema']]: raise ValueError('unexpected file')
            content = b''.join(json_bytes(row) + b'\n' for row in rows)
            write_bytes(path / name, content)
            files[name] = hashlib.sha256(content).hexdigest()
        manifest = clean_json({**manifest, 'files': files,
                               'created_at': datetime.now(timezone.utc).isoformat()})
        publish_json(path, 'manifest.json', manifest)
        return manifest
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ObservationError('output_failed') from None


def read_artifact(path, schema, cls):
    path = Path(path)
    try:
        if path.is_symlink() or (path/'manifest.json').is_symlink(): raise ValueError('symlink')
        manifest = json.loads((path/'manifest.json').read_bytes())
        if manifest['schema'] != schema or set(manifest['files']) != set(FILES[schema]):
            raise ValueError('schema')
        records = {}
        for name in FILES[schema]:
            if (path/name).is_symlink(): raise ValueError('symlink')
            content = (path/name).read_bytes()
            if hashlib.sha256(content).hexdigest() != manifest['files'][name]: raise ValueError('changed artifact')
            records[name] = [json.loads(line) for line in content.splitlines() if line.strip()]
        return cls(path, manifest, records)
    except (OSError, ValueError, KeyError, TypeError):
        raise ObservationError('source_unreadable') from None


def write_plan(path, data):
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
        publish_json(path, 'plan.json', data)
    except (OSError, ValueError, TypeError):
        raise ObservationError('output_failed') from None
    return path


def publish_json(path, name, value):
    """Only a fully written file can become the artifact's completion marker."""
    pending = path / (name + '.pending')
    write_bytes(pending, json_bytes(value))
    # link does not replace an existing file, including a concurrent writer's.
    os.link(pending, path / name)
    pending.unlink()
