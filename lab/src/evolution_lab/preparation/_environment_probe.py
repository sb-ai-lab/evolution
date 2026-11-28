"""Stdlib-only verifier for a declared Python environment.

This file is executed directly with ``declared_python -I -S``.  It deliberately
does not import Lab, Evolution, a target, or any wheel member under inspection.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import sysconfig
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


# Mirrors _manifest_schema.DIAGNOSIS_LIMIT; this script cannot import Lab.
_DIAGNOSIS_LIMIT = 400


class _Unavailable(Exception):
    pass


class _Invalid(Exception):
    def __init__(self, detail: str):
        self.detail = "".join(char if char.isprintable() else "?" for char in detail)[:_DIAGNOSIS_LIMIT]
        super().__init__(self.detail)


def _label(value: str | None) -> str:
    """Metadata labels must not smuggle host paths or exception text into detail."""
    return value if value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*", value) else "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise _Unavailable from exc
    return digest.hexdigest()


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _deduplicate_site_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Keep distinct existing final directories by device/inode, not spelling."""
    result: list[Path] = []
    identities: set[tuple[int, int]] = set()
    for path in paths:
        try:
            if path.is_symlink() or not path.is_dir():
                raise _Unavailable
            status = path.stat()
        except OSError:
            raise _Unavailable from None
        identity = (status.st_dev, status.st_ino)
        if identity not in identities:
            identities.add(identity)
            result.append(path)
    if not result:
        raise _Unavailable
    return tuple(result)


def _site_paths(executable: Path) -> tuple[Path, ...]:
    """Locate the declared POSIX venv without relying on ``site`` startup."""
    root = executable.parent.parent
    config = root / "pyvenv.cfg"
    if executable.parent.name != "bin" or config.is_symlink() or not config.is_file():
        raise _Unavailable
    try:
        config.read_text(encoding="utf-8")
        schemes = sysconfig.get_scheme_names()
        scheme = "venv" if "venv" in schemes else "posix_venv"
        if scheme not in schemes:
            raise _Unavailable
        paths = sysconfig.get_paths(
            scheme=scheme,
            vars={
                "base": str(root),
                "platbase": str(root),
                "installed_base": str(root),
                "installed_platbase": str(root),
            },
        )
    except (OSError, KeyError, ValueError):
        raise _Unavailable from None
    return _deduplicate_site_paths(tuple(Path(paths[key]).absolute() for key in ("purelib", "platlib")))


def _installed_distribution(name: str, site_paths: tuple[Path, ...]):
    try:
        matches = [
            distribution
            for distribution in metadata.distributions(path=[str(path) for path in site_paths])
            if _name(distribution.metadata.get("Name", "")) == _name(name)
        ]
    except (OSError, ValueError):
        raise _Unavailable from None
    if not matches:
        raise _Invalid(f"{_label(_name(name))}: not installed in the declared environment")
    if len(matches) != 1:
        raise _Invalid(f"{_label(_name(name))}: installed distribution is ambiguous")
    return matches[0]


def _inside_site(path: Path, site_paths: tuple[Path, ...]) -> bool:
    try:
        return any(path.absolute().is_relative_to(site) for site in site_paths)
    except OSError:
        return False


def _wheel(path: Path, site_paths: tuple[Path, ...]) -> dict[str, str]:
    before = _sha256(path)
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise _Unavailable
            for member in names:
                parts = PurePosixPath(member)
                if parts.is_absolute() or ".." in parts.parts or "\\" in member:
                    raise _Unavailable
                if any(part.endswith(".data") for part in parts.parts):
                    raise _Unavailable
            metadata_members = [member for member in names if member.endswith(".dist-info/METADATA")]
            if len(metadata_members) != 1:
                raise _Unavailable
            info = BytesParser().parsebytes(archive.read(metadata_members[0]))
            name, version = info.get("Name"), info.get("Version")
            if not name or not version:
                raise _Unavailable
            distribution = _installed_distribution(name, site_paths)
            label = f"{_label(_name(name))} {_label(version)}"
            if _name(distribution.metadata.get("Name", "")) != _name(name) or distribution.version != version:
                raise _Invalid(
                    f"{_label(_name(name))}: installed version {_label(distribution.version)} "
                    f"differs from the wheel's {_label(version)}"
                )
            module_paths: dict[str, str] = {}
            for member in names:
                if member.endswith("/") or member.endswith(".dist-info/RECORD"):
                    continue
                installed = Path(distribution.locate_file(member))
                differs = f"{label}: installed file {member} differs from its wheel"
                try:
                    if not _inside_site(installed, site_paths) or installed.is_symlink() or installed.read_bytes() != archive.read(member):
                        raise _Invalid(differs)
                    parts = PurePosixPath(member).parts
                    if len(parts) == 2 and parts[1] == "__init__.py":
                        module_paths[parts[0]] = str(installed.absolute())
                except FileNotFoundError:
                    raise _Invalid(f"{label}: installed file {member} is missing") from None
                except OSError:
                    raise _Invalid(differs) from None
    except _Invalid:
        raise
    except (OSError, BadZipFile, ValueError):
        raise _Unavailable from None
    if _sha256(path) != before:
        raise _Invalid("the wheel changed during verification")
    return {
        "path": str(path.absolute()),
        "name": _name(name),
        "version": version,
        "wheel_sha256": before,
        "module_paths": module_paths,
    }


def _input() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        raise _Unavailable from None
    if not isinstance(value, dict) or set(value) != {"executable", "wheel_paths", "runtime_files"}:
        raise _Unavailable
    if not isinstance(value["executable"], str):
        raise _Unavailable
    if not all(isinstance(value[key], list) and all(isinstance(item, str) for item in value[key]) for key in ("wheel_paths", "runtime_files")):
        raise _Unavailable
    return value


def _receipt(value: dict[str, object]) -> dict[str, object]:
    executable = Path(value["executable"])
    if Path(sys.executable).absolute() != executable.absolute():
        raise _Invalid("the declared executable is not this service's interpreter")
    site_paths = _site_paths(executable)
    wheels = [_wheel(Path(item), site_paths) for item in value["wheel_paths"]]
    if len({wheel["name"] for wheel in wheels}) != len(wheels):
        raise _Invalid("the declared environment contains duplicate distributions")
    runtime_files = []
    for item in value["runtime_files"]:
        path = Path(item)
        if path.is_symlink() or not path.is_file():
            raise _Unavailable
        runtime_files.append({"path": str(path.absolute()), "sha256": _sha256(path)})
    if len({item["path"] for item in runtime_files}) != len(runtime_files):
        raise _Invalid("the declared environment contains duplicate runtime files")
    return {
        "executable": str(executable.absolute()),
        "executable_sha256": _sha256(executable),
        "python_version": platform.python_version(),
        "archive_wheels": sorted(wheels, key=lambda item: item["name"]),
        "runtime_files": sorted(runtime_files, key=lambda item: item["path"]),
        "platform": {"system": platform.system(), "machine": platform.machine()},
    }


def main() -> int:
    try:
        print(json.dumps({"status": "ok", "receipt": _receipt(_input())}, sort_keys=True))
    except _Invalid as error:
        print(json.dumps({"status": "environment_invalid", "detail": error.detail}, sort_keys=True))
    except _Unavailable:
        print('{"status":"environment_unavailable"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
