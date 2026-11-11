#!/usr/bin/env python3
"""Validate distribution members against the declared sources of this checkout."""

from __future__ import annotations

import fnmatch
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
_METADATA = {"METADATA", "WHEEL", "RECORD", "entry_points.txt"}


def _members(path: Path) -> list[tuple[str, bytes | None]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return [
                (
                    m.filename,
                    None
                    if stat.S_ISLNK(m.external_attr >> 16)
                    else b""
                    if m.is_dir()
                    else archive.read(m),
                )
                for m in archive.infolist()
            ]
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            members = []
            for member in archive.getmembers():
                if member.isdir():
                    members.append((member.name.rstrip("/") + "/", b""))
                elif member.isfile():
                    stream = archive.extractfile(member)
                    assert stream is not None
                    members.append((member.name, stream.read()))
                else:
                    members.append((member.name, None))
            return members
    raise ValueError(f"unsupported release artifact: {path}")


def _safe(name: str) -> bool:
    return (
        bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and all(
            p not in {"", ".", "..", "__pycache__"} and not p.endswith(".pyc")
            for p in name.rstrip("/").split("/")
        )
    )


def _matches(name: str, patterns: list[str]) -> bool:
    return any(
        name == pattern.lstrip("/").rstrip("/")
        or name.startswith(pattern.lstrip("/").rstrip("/") + "/")
        or fnmatch.fnmatchcase(name, pattern.lstrip("/"))
        for pattern in patterns
    )


def check_archive(path: Path) -> list[str]:
    """Return members outside the declared package or different from their source."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    targets = config["tool"]["hatch"]["build"]["targets"]
    wheel = path.suffix == ".whl"
    target = targets["wheel" if wheel else "sdist"]
    tracked: set[str] | None = None
    if (ROOT / ".git").exists():
        tracked = {
            p.decode()
            for p in subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"]).split(
                b"\0"
            )
            if p
        }
    distribution = re.sub(r"[-_.]+", "_", project["name"]).lower()
    license_paths = {
        p.relative_to(ROOT).as_posix()
        for pattern in project.get("license-files", ["LICENSE"])
        for p in ROOT.glob(pattern)
        if p.is_file()
    }
    violations = []
    for raw, data in _members(path):
        name = raw.removeprefix("./").rstrip("/")
        is_dir = raw.endswith("/")
        if not _safe(name) or data is None:
            violations.append(name)
            continue
        parts = PurePosixPath(name).parts
        source: str | None = None
        metadata = False
        if wheel:
            if parts[0].lower().startswith(distribution + "-") and parts[0].endswith(".dist-info"):
                relative = "/".join(parts[1:])
                metadata = relative in _METADATA
                if relative.startswith("licenses/"):
                    license_name = relative.removeprefix("licenses/")
                    if license_name in license_paths:
                        source = license_name
                if is_dir:
                    metadata = not relative or relative == "licenses"
            for package in target["packages"]:
                package = package.strip("/")
                root_name = PurePosixPath(package).name
                if parts[0] == root_name:
                    source = package + ("/" + "/".join(parts[1:]) if len(parts) > 1 else "")
        else:
            if len(parts) == 1 and is_dir:
                continue
            relative = "/".join(parts[1:])
            metadata = relative == "PKG-INFO" and not is_dir
            if relative == ".gitignore" or _matches(relative, target["only-include"]):
                source = relative
        valid = metadata
        if source is not None and not _matches(source, target.get("exclude", [])):
            original = ROOT / source
            if is_dir:
                valid = original.is_dir() and (
                    tracked is None or any(p.startswith(source + "/") for p in tracked)
                )
            else:
                valid = (
                    (tracked is None or source in tracked)
                    and original.is_file()
                    and not original.is_symlink()
                    and original.read_bytes() == data
                )
        if not valid:
            violations.append(name)
    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print("usage: check_release_artifacts.py ARTIFACT [...]", file=sys.stderr)
        return 2
    failed = False
    for path in paths:
        violations = check_archive(path)
        if violations:
            failed = True
            print(f"{path}: unexpected distribution members", file=sys.stderr)
            for name in violations:
                print(f"  {name}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
