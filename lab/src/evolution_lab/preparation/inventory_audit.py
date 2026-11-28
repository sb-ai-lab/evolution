"""Check schema-aware relocation against the core manifest dependency inventory (S6 §3)."""
from __future__ import annotations

import re
from pathlib import Path

from evolution.tree import manifest_dependencies

from .models import PreparationError
from .relocation import local_path, operand_is_path


def dotted_to_pointer(field: str) -> str:
    return "/" + re.sub(r"\[(\d+)\]", r".\1", field).replace(".", "/")


def _unsupported() -> None:
    raise PreparationError("unsupported_input", "bundle")


def _raw_section(raw: dict, pointer: str) -> dict:
    parts = pointer.strip("/").split("/")
    node = raw
    for part in parts[: parts.index("argv")]:
        node = node[part]
    return node


def audit_manifest_relocation(manifest, *, relocator, document: Path, raw: dict, pinned: set[str]) -> None:
    """Refuse the campaign Bundle unless every declared core dependency is
    accounted for by relocation (S6 spec §3.3's ordered, first-match rules).
    """
    for dependency in manifest_dependencies(manifest).dependencies:
        pointer = dotted_to_pointer(dependency.field)
        if dependency.kind == "executable":
            if dependency.declared not in pinned:
                _unsupported()
        elif dependency.role == "artifacts" and dependency.base == "seed_root":
            continue
        elif dependency.base == "packaged":
            continue
        elif dependency.kind == "argv_operand":
            section = _raw_section(raw, pointer)
            cwd = local_path(section["cwd"], document.parent)
            if operand_is_path(relocator, document, pointer, dependency.declared, cwd) and \
                    (document, pointer) not in relocator.relocated:
                _unsupported()
        elif (document, pointer) not in relocator.relocated:
            _unsupported()
