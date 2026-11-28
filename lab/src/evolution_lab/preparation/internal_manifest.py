"""Read Lab's workflow-less internal entries without changing their saved bytes."""
from __future__ import annotations

import json
from pathlib import Path

from evolution.tree import ManifestError, ManifestV2, conformance_manifest


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")


def load_internal_manifest(path: Path) -> ManifestV2:
    """Load .lab/conformance.json or .lab/probe-policy.json, including dev57's schema.

    Keep native loader errors for file/JSON failures; callers retain their existing
    preparation/Owner mappings. Campaign manifests must still use load_manifest_v2.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ManifestError("bad_path", "manifest") from error
    try:
        document = json.loads(text, parse_constant=_reject_constant)
    except ValueError as error:
        raise ManifestError("bad_type", "$") from error
    if not isinstance(document, dict):
        raise ManifestError("bad_type", "$")
    document.pop("schema", None)
    return conformance_manifest(document, base=path.parent)
