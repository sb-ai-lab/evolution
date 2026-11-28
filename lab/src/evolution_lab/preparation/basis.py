"""The fixed-evaluation input identity of an immutable stored Bundle (ADR-0028)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evolution.tree import ManifestError, load_manifest_v2

from evolution_lab._manifest_schema import is_campaign_manifest

from .internal_manifest import load_internal_manifest
from .models import PreparationError, StoredBundle
from .storage import canonical_json_bytes


def _document(path: Path) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError('non-finite JSON')

    result = json.loads(path.read_bytes(), object_pairs_hook=unique, parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError('document must be an object')
    return result


def measurement_basis(stored_bundle: StoredBundle) -> str | None:
    """Hash saved logical inputs without resolving host paths or writing state.

    Core's public loader is the validity gate; its resolved paths never enter the
    identity. New fixed-evaluation Bundle reads must extend this inclusion boundary
    and the independent consumer read-set guard (ADR-0028).
    """
    descriptor = stored_bundle.descriptor
    try:
        manifest_path = stored_bundle.path / descriptor.conformance_entrypoint
        manifest = _document(manifest_path)
        if descriptor.source_kind == 'campaign':
            if not is_campaign_manifest(load_manifest_v2(manifest_path)):
                return None
        else:
            load_internal_manifest(manifest_path)
        # Core's workflow.environment/scorer are top-level saved manifest fields.
        manifest_projection = {
            key: manifest[key] for key in (
                'schema', 'environment', 'scorer', 'seed_root', 'target', 'artifacts', 'corpus',
            ) if key in manifest
        }
        launch = _document(stored_bundle.path / descriptor.launch)
        launch['limits'] = {
            key: value for key, value in launch['limits'].items()
            if key not in {'max_instances', 'max_concurrent_sessions'}
        }
        payload = {
            'manifest': manifest_projection,
            'input': (
                None if descriptor.source_kind == 'campaign'
                else _document(stored_bundle.path / descriptor.entrypoint)
            ),
            'entries': [
                entry.to_payload() for entry in descriptor.entries
                if entry.path not in {descriptor.entrypoint, descriptor.launch}
            ],
            'launch': launch,
            'env': dict(descriptor.env),
            'secret_names': list(descriptor.secret_names),
            'environments': [environment.to_payload() for environment in descriptor.environments],
            'source_kind': descriptor.source_kind,
            'conformance_entrypoint': descriptor.conformance_entrypoint,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    except (ManifestError, PreparationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        # An unreadable input has no reusable identity, including older Bundles
        # whose manifest is no longer admitted by the exact producing pair.
        return None
