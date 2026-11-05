#!/usr/bin/env python3
"""Fail when a wheel or sdist contains checkout-only state, runtime state or dormant GUI source."""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

_FORBIDDEN_COMPONENTS = {
    ".agents",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".traces",
    "__pycache__",
    "experiments",
    "results",
    "runs",
    "tasks",
    "tests",
}
_FORBIDDEN_EXACT = {"evolution/core/experiments.py"}
# Retired GUI code must never re-enter a product artifact.
_FORBIDDEN_PREFIXES = ("evolution/gui/", "src/evolution/gui/")
#: The same two roots without a trailing slash: an archive member can name a
#: directory exactly this way (no trailing `/`), which the prefix check above
#: does not match.
_FORBIDDEN_BARE = {"evolution/gui", "src/evolution/gui"}
_PRIVATE_DOC_ROOTS = ("docs/development/", "docs/adr/", "docs/pdr/", "docs/notes/")
_PRIVATE_DOC_FILES = {"AGENTS.md", "CLAUDE.md", "ROADMAP.md", "UBIQUITOUS_LANGUAGE.md"}


def _members(path: Path) -> list[tuple[str, bytes]]:
    if path.suffix == ".whl" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return [
                (name, archive.read(name) if name.endswith(".md") else b"")
                for name in archive.namelist()
            ]
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            members = []
            for member in archive.getmembers():
                stream = (
                    archive.extractfile(member)
                    if member.isfile() and member.name.endswith(".md")
                    else None
                )
                members.append((member.name, stream.read() if stream else b""))
            return members
    raise ValueError(f"unsupported release artifact: {path}")


def check_archive(path: Path) -> list[str]:
    """Return sorted forbidden members from one wheel or source archive."""
    violations: list[str] = []
    for raw, content in _members(Path(path)):
        name = raw.removeprefix("./").rstrip("/")
        if not name:
            continue
        parts = PurePosixPath(name).parts
        without_sdist_root = "/".join(parts[1:]) if len(parts) > 1 else name
        if (
            any(part in _FORBIDDEN_COMPONENTS or part.endswith(".pyc") for part in parts)
            or without_sdist_root in _FORBIDDEN_EXACT
            or name in _FORBIDDEN_EXACT
            or without_sdist_root.startswith(_FORBIDDEN_PREFIXES)
            or name.startswith(_FORBIDDEN_PREFIXES)
            or without_sdist_root in _FORBIDDEN_BARE
            or name in _FORBIDDEN_BARE
            or name.startswith(_PRIVATE_DOC_ROOTS)
            or without_sdist_root.startswith(_PRIVATE_DOC_ROOTS)
            or parts[-1] in _PRIVATE_DOC_FILES
            or re.search(
                rb"(?im)^(?:status: (?:Accepted|Superseded|Retired)|# .*Implementation Plan\s*$)",
                content,
            )
        ):
            violations.append(name)
    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    paths = [Path(value) for value in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print("usage: check_release_artifacts.py ARTIFACT [...]", file=sys.stderr)
        return 2
    failed = False
    for path in paths:
        violations = check_archive(path)
        if not violations:
            continue
        failed = True
        print(f"{path}: forbidden release members", file=sys.stderr)
        for member in violations:
            print(f"  {member}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
